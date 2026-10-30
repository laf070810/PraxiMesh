"""Startup DB/filesystem fencing; GPU maintenance is a test double, not proof."""

import time
import uuid

import pytest

from core import gpu_resources
from core.gpu_startup import recover_controller, startup_identity
from core.models import GPUDevice
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from judge.gpu_namespace import JobWorkspace
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_resources import DEVICE, FINGERPRINT, queued
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import guard as guard

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def startup(guard, gpu_version, user, tmp_path, monkeypatch):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
    adapter.target = {"uuid": DEVICE}
    adapter.fingerprint = FINGERPRINT
    adapter.directory = tmp_path / "jobs"
    events = []
    monkeypatch.setattr(OwnedJobService, "inspect", lambda _self: None)
    monkeypatch.setattr(OwnedJobService, "populated", lambda _self: False)
    monkeypatch.setattr(
        OwnedJobService,
        "stop",
        lambda self: (
            events.append(("stopped", self.unit))
            or {"unit": self.unit, "stopped": True, "cgroup_empty": True}
        ),
    )

    def recover(_self, service, *, deadline, on_tick):
        assert service.purpose == "maintenance"
        assert 0 < deadline - time.monotonic() <= 90
        on_tick()
        events.append(("fixed-recovery", service.unit))
        return {"scope": "test double, no GPU or readiness evidence"}

    monkeypatch.setattr(GPUExecutionAdapter, "recover", recover)
    return job, adapter, events


def test_startup_stops_only_persisted_and_fixed_services_and_remains_quarantined(guard, startup):
    job, adapter, events = startup
    startup_job, startup_reservation = startup_identity(DEVICE)
    services = [
        OwnedJobService(guard, job.id, job.lease_token, purpose=purpose)
        for purpose in ("task", "maintenance", "recovery")
    ]
    services += [
        OwnedJobService(guard, startup_job, startup_reservation, purpose=purpose)
        for purpose in ("maintenance", "recovery")
    ]
    paths = []
    for service in services:
        with JobWorkspace(adapter.directory, service) as workspace:
            (workspace.path / "fixed-artifact").write_text("owned")
            paths.append(workspace.path)
    unrelated = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
    with JobWorkspace(adapter.directory, unrelated) as unknown:
        marker = unknown.path / "keep"
        marker.write_text("unrelated")
        report = recover_controller(guard, "scheduler-test", adapter)
        assert report["recovered"] and report["ready"] is False
        assert report["previous_job"] == str(job.id)
        assert all(not path.exists() for path in paths)
        assert marker.read_text() == "unrelated"
        assert not any(unit == unrelated.unit for _action, unit in events)
    first_recovery = next(
        i for i, (action, _unit) in enumerate(events) if action == "fixed-recovery"
    )
    assert first_recovery == len(services)
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "quarantined" and device.checked_at is None
    assert (device.job_id, device.reservation_token) == (job.id, job.lease_token)
    assert gpu_resources.claim(guard) is None
    job.refresh_from_db()
    assert job.status == "running" and job.execution_started_at is None


def test_recovery_without_a_job_has_stable_maintenance_identity(guard, startup):
    job, adapter, events = startup
    gpu_resources.quarantine(guard, job)
    gpu_resources.confirm_recovered(guard, job_id=job.id, reservation_token=job.lease_token)
    first = recover_controller(guard, "scheduler-test", adapter)
    second = recover_controller(guard, "scheduler-test", adapter)
    assert first["previous_job"] is None and second["previous_job"] is None
    assert first["maintenance_unit"] == second["maintenance_unit"]
    assert len([action for action, _unit in events if action == "fixed-recovery"]) == 2
    assert GPUDevice.objects.get(pk=DEVICE).state == "quarantined"


def test_new_host_lock_owner_adopts_old_work_without_releasing_its_reservation(guard, startup):
    job, adapter, events = startup
    startup_job, startup_reservation = startup_identity(DEVICE)
    old_owner = guard.owner_token
    old_units = []
    old_paths = []
    for job_id, reservation, purpose in (
        (job.id, job.lease_token, "task"),
        (startup_job, startup_reservation, "maintenance"),
    ):
        service = OwnedJobService(guard, job_id, reservation, purpose=purpose)
        old_units.append(service.unit)
        with JobWorkspace(adapter.directory, service) as workspace:
            old_paths.append(workspace.path)
    guard.close()
    with DeviceLock(guard.directory, DEVICE) as replacement:
        assert replacement.owner_token != old_owner
        report = recover_controller(replacement, "replacement-worker", adapter)
        assert report["maintenance_unit"] == old_units[1]
        assert all(("stopped", unit) in events for unit in old_units)
        assert all(not path.exists() for path in old_paths)
        device = GPUDevice.objects.get(pk=DEVICE)
        assert device.owner_token == replacement.owner_token
        assert device.worker_id == "replacement-worker"
        assert device.state == "quarantined" and device.checked_at is None
        assert (device.job_id, device.reservation_token) == (job.id, job.lease_token)
        assert not gpu_resources.available(DEVICE, FINGERPRINT)
        assert gpu_resources.claim(replacement) is None
        job.refresh_from_db()
        assert job.budget_state == "reserved" and job.lease_token == device.reservation_token


def test_failed_old_service_stop_preserves_directories_and_skips_new_gpu_work(
    guard, startup, monkeypatch
):
    job, adapter, events = startup
    with JobWorkspace(
        adapter.directory, OwnedJobService(guard, job.id, job.lease_token)
    ) as workspace:

        def stop(service):
            events.append(("stopped", service.unit))
            if service.purpose == "task":
                raise SandboxUnavailable("fixed identity mismatch")
            return {"unit": service.unit, "stopped": True}

        monkeypatch.setattr(OwnedJobService, "stop", stop)
        with pytest.raises(SandboxUnavailable, match="could not be stopped"):
            recover_controller(guard, "scheduler-test", adapter)
        assert workspace.path.exists()
    assert len(events) == 5 and all(action == "stopped" for action, _unit in events)
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_changed_database_owner_does_not_prevent_stopping_old_services(guard, startup, monkeypatch):
    job, adapter, events = startup
    new_owner = uuid.uuid4()

    def stop(service):
        GPUDevice.objects.filter(pk=DEVICE).update(owner_token=new_owner)
        events.append(("stopped", service.unit))
        return {"unit": service.unit, "stopped": True}

    monkeypatch.setattr(OwnedJobService, "stop", stop)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        recover_controller(guard, "scheduler-test", adapter)
    assert len(events) == 5 and all(action == "stopped" for action, _unit in events)
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.owner_token == new_owner and device.job_id == job.id


def test_changed_slot_during_maintenance_cannot_be_released(guard, startup, monkeypatch):
    _job, adapter, events = startup
    replacement = uuid.uuid4(), uuid.uuid4()

    def changed(_self, service, *, deadline, on_tick):
        GPUDevice.objects.filter(pk=DEVICE).update(
            job_id=replacement[0], reservation_token=replacement[1]
        )
        on_tick()

    monkeypatch.setattr(GPUExecutionAdapter, "recover", changed)
    with pytest.raises(gpu_resources.DeviceOwnershipError, match="reservation changed"):
        recover_controller(guard, "scheduler-test", adapter)
    device = GPUDevice.objects.get(pk=DEVICE)
    assert (device.job_id, device.reservation_token) == replacement
    assert device.state == "quarantined"
    assert events[-1][0] == "stopped"


def test_deleted_account_still_recovers_exact_previous_reservation(guard, startup):
    job, adapter, _events = startup
    job.user.delete()
    report = recover_controller(guard, "scheduler-test", adapter)
    assert report["previous_job"] == str(job.id)
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_fixed_gpu_probe_failure_never_announces_ready(guard, startup, monkeypatch):
    job, adapter, events = startup

    def failure(_self, service, **_kwargs):
        raise SandboxUnavailable("fixed maintenance failure")

    monkeypatch.setattr(GPUExecutionAdapter, "recover", failure)
    with pytest.raises(SandboxUnavailable, match="maintenance failure"):
        recover_controller(guard, "scheduler-test", adapter)
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "quarantined" and device.job_id == job.id
    assert events[-1][0] == "stopped"


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, -1])
def test_invalid_startup_budget_does_not_adopt_or_stop(guard, startup, deadline):
    job, adapter, events = startup
    with pytest.raises(ValueError, match="deadline"):
        recover_controller(guard, "scheduler-test", adapter, deadline=deadline)
    assert not events
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "reserved" and device.job_id == job.id
