"""Real DB fencing with a modeled Docker engine; hardware claims use CPU acceptance."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from core import gpu_resources
from core.gpu_container_recovery import BoundContainerRecovery
from core.gpu_startup import recover_controller, startup_identity
from core.models import GPUDevice, Submission
from judge.container_devices import PROFILE as DEVICE_PROFILE
from judge.container_journal import ContainerJournal
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable
from tests.test_container_driver import bundle as bundle
from tests.test_container_gpu import device_intent
from tests.test_container_journal import Engine, intent
from tests.test_gpu_resources import DEVICE, FINGERPRINT, queued
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import guard as guard
from tests.test_gpu_startup import startup as startup

pytestmark = pytest.mark.django_db(transaction=True)


def device_selection(guard):
    return {
        "schema": 1,
        "profile": DEVICE_PROFILE,
        "uuid": guard.device_uuid,
        "driver": "590.48.01",
        "minor": 0,
        "uvm_major": 509,
    }


@pytest.fixture(params=[False, True], ids=["cpu-v1", "device-v3"])
def pending(guard, gpu_version, user, tmp_path, request, bundle):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    engine = Engine()
    with ContainerJournal(tmp_path / "journal", guard, engine) as journal:
        if request.param:
            service, _state = device_intent(
                journal, bundle[1], device_selection(guard), job.id, job.lease_token
            )
        else:
            service, _state = intent(journal, job.id, job.lease_token)
        yield journal, job, service, engine


def finish(recovery, job):
    return recovery.finish(job_id=job.id, reservation_token=job.lease_token)


@pytest.mark.parametrize("consumed", [False, True])
def test_recovery_preserves_reserved_or_consumed_budget_and_never_announces_ready(
    guard, pending, consumed
):
    journal, job, _service, engine = pending
    if consumed:
        assert gpu_resources.begin_execution(guard, job)
    gpu_resources.quarantine(guard)
    recovery = BoundContainerRecovery(journal)
    stopped = recovery.stop()
    assert stopped["stopped"] and journal.load()["container_id"] is not None
    assert engine.state is not None and not engine.state["State"]["Running"]
    assert not any(call[:2] == ("container", "rm") for call in engine.calls)
    result = finish(recovery, job)
    assert result["had_record"] and result["cleanup"]["removed"] and result["ready"] is False
    assert journal.load() is None and engine.state is None
    row = GPUDevice.objects.get(pk=DEVICE)
    assert row.state == "quarantined" and row.checked_at is None
    assert (row.job_id, row.reservation_token) == (job.id, job.lease_token)
    assert not gpu_resources.available(DEVICE, FINGERPRINT) and gpu_resources.claim(guard) is None
    job.refresh_from_db()
    assert job.status == "running" and not job.verdict
    assert job.budget_state == ("consumed" if consumed else "reserved")


def test_explicit_stop_is_required_before_archival(guard, pending):
    journal, job, _service, engine = pending
    gpu_resources.quarantine(guard)
    with pytest.raises(SandboxUnavailable, match="Stop the original"):
        finish(BoundContainerRecovery(journal), job)
    assert engine.state["State"]["Running"]


@pytest.mark.parametrize("defect", ["owner", "reservation", "not-quarantined"])
def test_database_fence_failure_retains_stopped_container_record(guard, pending, defect):
    journal, job, _service, engine = pending
    gpu_resources.quarantine(guard)
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    before = journal.load()
    fields = {
        "owner": {"owner_token": uuid.uuid4()},
        "reservation": {"reservation_token": uuid.uuid4()},
        "not-quarantined": {"state": "reserved", "checked_at": timezone.now()},
    }[defect]
    GPUDevice.objects.filter(pk=DEVICE).update(**fields)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        finish(recovery, job)
    assert journal.load() == before
    assert engine.state is not None and not engine.state["State"]["Running"]
    assert not any(call[:2] == ("container", "rm") for call in engine.calls)
    for name, value in fields.items():
        assert getattr(GPUDevice.objects.get(pk=DEVICE), name) == value


def test_mismatched_persisted_task_is_stopped_but_not_removed_or_released(guard, pending):
    journal, job, _service, engine = pending
    gpu_resources.quarantine(guard)
    other = uuid.uuid4(), uuid.uuid4()
    GPUDevice.objects.filter(pk=DEVICE).update(job_id=other[0], reservation_token=other[1])
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    with pytest.raises(gpu_resources.DeviceOwnershipError, match="does not match"):
        recovery.finish(job_id=other[0], reservation_token=other[1])
    assert journal.load()["job"] == str(job.id) and engine.state is not None
    row = GPUDevice.objects.get(pk=DEVICE)
    assert (row.job_id, row.reservation_token) == other and row.state == "quarantined"


def test_account_deletion_does_not_discard_the_physical_reservation(guard, pending):
    journal, job, _service, engine = pending
    slot = job.id, job.lease_token
    job.user.delete()
    assert not Submission.objects.filter(pk=slot[0]).exists()
    gpu_resources.quarantine(guard)
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    assert finish(recovery, job)["ready"] is False and engine.state is None
    row = GPUDevice.objects.get(pk=DEVICE)
    assert (row.job_id, row.reservation_token) == slot and row.state == "quarantined"


@pytest.mark.parametrize("purpose", ["maintenance", "recovery", "task"])
@pytest.mark.parametrize("device_task", [False, True])
def test_idle_recovery_accepts_only_fixed_startup_maintenance(
    guard, tmp_path, purpose, device_task, bundle
):
    engine = Engine()
    with ContainerJournal(tmp_path / "journal", guard, engine) as journal:
        slot = startup_identity(DEVICE)
        if device_task:
            device_intent(journal, bundle[1], device_selection(guard), *slot, purpose=purpose)
        else:
            intent(journal, *slot, purpose=purpose)
        gpu_resources.quarantine(guard)
        recovery = BoundContainerRecovery(journal)
        recovery.stop()
        if purpose == "task":
            with pytest.raises(gpu_resources.DeviceOwnershipError):
                recovery.finish(job_id=None, reservation_token=None)
            assert journal.load() is not None
        else:
            result = recovery.finish(job_id=None, reservation_token=None)
            assert result["cleanup"]["removed"] and result["ready"] is False
        assert GPUDevice.objects.get(pk=DEVICE).state == "quarantined"


def test_record_change_after_stop_is_not_adopted_for_cleanup(guard, pending):
    journal, job, _service, engine = pending
    gpu_resources.quarantine(guard)
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    record = journal.load()
    changed = {**record, "image": "sha256:" + "f" * 64}
    journal.write(journal.filename, changed)
    with pytest.raises(SandboxUnavailable, match="record changed"):
        finish(recovery, job)
    assert journal.load() == changed and engine.state is not None


def test_archive_failure_can_be_retried_without_releasing_slot(guard, pending, monkeypatch):
    journal, job, _service, engine = pending
    gpu_resources.quarantine(guard)
    original = journal.write

    def fail(name, value, **kwargs):
        if name.endswith(".done.json"):
            raise OSError("fixed archive interruption")
        return original(name, value, **kwargs)

    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    monkeypatch.setattr(journal, "write", fail)
    with pytest.raises(OSError, match="archive interruption"):
        finish(recovery, job)
    assert engine.state is None and journal.load()["phase"] == "removed"
    monkeypatch.setattr(journal, "write", original)
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    assert finish(recovery, job)["cleanup"]["removed"]
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_changed_owner_after_archival_cannot_be_reported_as_success(guard, pending, monkeypatch):
    journal, job, _service, _engine = pending
    gpu_resources.quarantine(guard)
    recovery = BoundContainerRecovery(journal)
    recovery.stop()
    original = journal.recover
    replacement = uuid.uuid4()

    def change():
        result = original()
        GPUDevice.objects.filter(pk=DEVICE).update(owner_token=replacement)
        return result

    monkeypatch.setattr(journal, "recover", change)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        finish(recovery, job)
    assert journal.load() is None and list(journal.directory.glob("*.done.json"))
    row = GPUDevice.objects.get(pk=DEVICE)
    assert row.owner_token == replacement and row.job_id == job.id and row.state == "quarantined"


def test_existing_startup_cleans_container_before_independent_gpu_maintenance(
    guard, startup, tmp_path, monkeypatch
):
    job, adapter, events = startup
    with ContainerJournal(tmp_path / "journal", guard, Engine()) as journal:
        intent(journal, job.id, job.lease_token)
        original = GPUExecutionAdapter.recover

        def maintenance(self, *args, **kwargs):
            assert journal.load() is None and journal.engine.state is None
            return original(self, *args, **kwargs)

        monkeypatch.setattr(GPUExecutionAdapter, "recover", maintenance)
        report = recover_controller(guard, "scheduler-test", adapter, container_journal=journal)
        assert report["container_recovery"]["cleanup"]["removed"] and report["ready"] is False
        assert events[-2][0] == "fixed-recovery" and events[-1][0] == "stopped"
        assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_mismatched_container_blocks_host_maintenance_after_all_known_stops(
    guard, startup, tmp_path
):
    _job, adapter, events = startup
    with ContainerJournal(tmp_path / "journal", guard, Engine()) as journal:
        intent(journal)
        with pytest.raises(gpu_resources.DeviceOwnershipError, match="does not match"):
            recover_controller(guard, "scheduler-test", adapter, container_journal=journal)
        assert len(events) == 5 and all(action == "stopped" for action, _unit in events)
        assert journal.load() is not None and not journal.engine.state["State"]["Running"]


def test_actual_postgres_lock_timeout_stops_original_without_archiving(guard, pending):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL lock contention")
    journal, job, _service, engine = pending
    adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
    adapter.target = {"uuid": DEVICE}
    adapter.fingerprint = FINGERPRINT

    def recover():
        try:
            return recover_controller(guard, "scheduler-test", adapter, container_journal=journal)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            GPUDevice.objects.select_for_update().get(pk=DEVICE)
            start = time.monotonic()
            with pytest.raises(DatabaseError) as error:
                executor.submit(recover).result(timeout=4)
            assert error.value.__cause__.sqlstate == "55P03" and time.monotonic() - start < 3
            assert engine.state is not None and not engine.state["State"]["Running"]
    assert journal.load()["container_id"] is not None
    row = GPUDevice.objects.get(pk=DEVICE)
    assert row.job_id == job.id and row.reservation_token == job.lease_token
    assert row.state == "reserved"


def test_replacement_lock_object_cannot_use_an_old_open_journal(guard, pending):
    journal, _job, _service, _engine = pending
    guard.close()
    with DeviceLock(guard.directory, DEVICE) as replacement:
        adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
        adapter.target, adapter.fingerprint = {"uuid": DEVICE}, FINGERPRINT
        with pytest.raises(ValueError, match="same held device lock"):
            recover_controller(replacement, "replacement", adapter, container_journal=journal)
