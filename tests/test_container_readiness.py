"""Real locks, private files and DB transactions; all GPU execution is modeled."""

import copy
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from django.db import DatabaseError

from core import container_startup, gpu_readiness, gpu_resources
from core.gpu_admission import candidates
from core.gpu_startup import recover_controller
from core.models import GPUDevice
from judge.container_execution import KernelTask
from judge.container_isolation_protocol import target_identity
from judge.container_journal import ContainerJournal
from judge.gpu_device_lock import DeviceLock
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_receipt import CHECKS, ReceiptStore, check_receipt, validate_receipt
from judge.sandbox import SandboxUnavailable
from tests.gpu_evidence_fixtures import environment_fixture
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_gpu import device_intent
from tests.test_container_isolation import resource_report
from tests.test_container_journal import Engine
from tests.test_container_kernel_checks import check_report
from tests.test_container_lifecycle import cleanup
from tests.test_container_lifecycle import report as fault_report
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def startup(configured, tmp_path, monkeypatch, user, gpu_version):
    adapter, control = configured.adapter, configured.control
    engine = Engine()
    with DeviceLock(tmp_path / "locks", adapter.target["uuid"]) as guard:
        with ContainerJournal(tmp_path / "journal", guard, engine) as journal:
            control.guard, control.journal = guard, journal
            control.saved.update(
                operator=[os.getuid(), os.getgid()],
                host_processes={"version": 1, "root": [1, 2], "boot": str(uuid.uuid4())},
                root=[2, 3],
                locks=[2, 4],
                journal=[2, 5],
                socket=[2, 6],
                engine=engine.identifier,
            )
            worker = "container-startup-test"
            gpu_resources.adopt(guard, worker, adapter.fingerprint)
            gpu_resources.confirm_recovered(guard, job_id=None, reservation_token=None)
            queued(user, gpu_version, evaluation_fingerprint=adapter.fingerprint)
            job = gpu_resources.claim(guard)
            device_intent(
                journal, adapter.driver_manifest, configured.selection, job.id, job.lease_token
            )
            directory = tmp_path / "receipts"
            state = SimpleNamespace(
                guard=guard,
                adapter=adapter,
                worker=worker,
                directory=directory,
                path=directory / (guard.device_uuid + ".json"),
                job=job,
                slot=(job.id, job.lease_token),
                events=[],
                tasks=configured.tasks,
                control=control,
                journal=journal,
                engine=engine,
                failure=None,
                occupied=False,
            )

            class Monitor:
                def __init__(self, original):
                    assert original is control

                def observe(self):
                    assert journal.load() is None
                    if state.occupied:
                        raise SandboxUnavailable("fixed external GPU process")
                    return []

            monkeypatch.setattr(container_startup, "ContainerDeviceMonitor", Monitor)
            monkeypatch.setattr(gpu_readiness, "ContainerDeviceMonitor", Monitor)
            create = KernelTask.__init__

            def task_init(task, *args, **kwargs):
                create(task, *args, **kwargs)
                task.cleanup = task.status = None

            monkeypatch.setattr(KernelTask, "__init__", task_init)

            def run(task, request, *, deadline, on_tick, on_begin):
                assert task.service.purpose == "maintenance"
                assert journal.load() is None and engine.state is None
                assert 0 < deadline - time.monotonic() <= 180
                on_tick()
                on_begin()
                row = GPUDevice.objects.get(pk=guard.device_uuid)
                assert row.state == "quarantined" and row.checked_at is None
                assert (row.job_id, row.reservation_token) == state.slot
                if request["event"] == "recover":
                    name = "initial-recovery" if not state.events else "fresh-recovery"
                elif request["event"] == "check-resources":
                    name = "resources"
                else:
                    name = request["event"].removeprefix("check-") + "/" + request["name"]
                if name != "fresh-recovery":
                    state.events.append(name)
                if name not in {"initial-recovery", "fresh-recovery"}:
                    assert not state.path.exists()
                if name == state.failure:
                    raise SandboxUnavailable("fixed startup stage failure")
                task.service.container_id = uuid.uuid4().hex + uuid.uuid4().hex
                task.status = {"nonce": uuid.uuid4().hex}
                task.cleanup = cleanup(
                    task.service.container_id, task.status["nonce"], guard.device_uuid
                )
                completed = {
                    "artifacts": {
                        "image": adapter.image,
                        "task_contract": adapter.environment["artifacts"]["task_contract"],
                        "resource_profile": adapter.environment["resource_profile"],
                        "limits": copy.deepcopy(adapter.environment["workload_limits"]),
                    }
                }
                if name == "resources":
                    report = resource_report(request, gpu=True)
                    report["checks"][0]["details"]["guest"]["gpu_nodes"] = target_identity(
                        adapter.target, configured.selection
                    )[1]
                    completed["report"] = report
                elif name.startswith("kernel/"):
                    completed["report"] = check_report(request)
                elif name.startswith("controller/"):
                    report = fault_report(request, device=guard.device_uuid)
                    report.update(
                        container_id=task.service.container_id,
                        nonce=task.status["nonce"],
                        cleanup=copy.deepcopy(task.cleanup),
                    )
                    completed["report"] = report
                return completed

            configured.run = run
            state.run = lambda **kwargs: gpu_readiness.initialize_controller(
                guard, worker, adapter, receipt_directory=directory, **kwargs
            )
            yield state


def test_full_container_sequence_publishes_only_after_original_slot_and_fresh_cleanup(
    startup, settings
):
    value = startup.run()
    assert startup.events == list(CHECKS)
    assert len(startup.tasks) == len(CHECKS) + 3
    assert len({task.service.container_id for task in startup.tasks}) == len(startup.tasks)
    assert value["schema"] == 2
    assert value["host"]["uid"] == os.getuid()
    assert value["container"]["journal"] == startup.journal.identity
    assert check_receipt(startup.directory, startup.guard, startup.worker, startup.adapter) == value
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "ready" and device.checked_at is not None
    assert device.environment == startup.adapter.environment and device.job_id is None
    startup.job.refresh_from_db()
    assert startup.job.status == "finished" and startup.job.budget_state == "released"
    settings.JUDGE_ENABLED = True
    settings.JUDGE_GPU_DEVICES = (device.pk,)
    assert [row.pk for row in candidates()] == [device.pk]
    # Admission stays read-only and is independent of the private receipt path.
    startup.path.unlink()
    assert [row.pk for row in candidates()] == [device.pk]
    with pytest.raises(FileNotFoundError):
        gpu_readiness.claim_ready(
            startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
        )
    assert not candidates()


@pytest.mark.parametrize(
    "stage",
    [
        "initial-recovery",
        "resources",
        "kernel/vector-reference",
        "kernel/attach-death",
        "controller/service-deadline",
        "fresh-recovery",
    ],
)
def test_incomplete_container_startup_withholds_receipt_and_preserves_slot(startup, stage):
    startup.failure = stage
    with pytest.raises(SandboxUnavailable, match="stage failure"):
        startup.run()
    assert not startup.path.exists()
    row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert row.state == "quarantined" and row.checked_at is None and row.environment == {}
    assert (row.job_id, row.reservation_token) == startup.slot


@pytest.mark.parametrize(
    "method,defect",
    [
        ("check_resources", "cpu"),
        ("check_resources", "challenge"),
        ("check_resources", "missing"),
        ("check_kernel", "cleanup"),
        ("check_kernel", "outer"),
        ("check_controller", "recovery"),
    ],
)
def test_incomplete_or_wrong_scope_reports_cannot_publish(startup, monkeypatch, method, defect):
    original = getattr(startup.adapter, method)

    def invalid(task, *args, **kwargs):
        report, environment = original(task, *args, **kwargs)
        if defect == "cpu":
            report["gpu"] = None
        elif defect == "challenge":
            report["challenge"] = uuid.uuid4().hex
        elif defect == "missing":
            report["checks"].pop()
        elif defect == "outer":
            task.cleanup["container"] = "f" * 64
        else:
            report.pop(defect)
        return report, environment

    monkeypatch.setattr(startup.adapter, method, invalid)
    with pytest.raises(SandboxUnavailable):
        startup.run()
    assert not startup.path.exists()
    assert GPUDevice.objects.get(pk=startup.guard.device_uuid).job_id == startup.job.id


@pytest.mark.parametrize(
    "field",
    [
        "boot",
        "operator",
        "mounts",
        "installation",
        "installation-file",
        "host-schema",
        "suite",
        "expired",
    ],
)
def test_private_binding_cannot_move_between_hosts_mounts_or_suites(startup, field):
    value = startup.run()
    if field == "boot":
        startup.control.saved["host_processes"]["boot"] = str(uuid.uuid4())
    elif field == "operator":
        startup.control.saved["operator"][0] += 1
    elif field == "mounts":
        startup.control.saved["root"][1] += 1
    elif field == "installation":
        startup.journal.identity["installation"] = uuid.uuid4().hex
    elif field == "installation-file":
        startup.journal.write(
            "installation.json", {**startup.journal.identity, "installation": uuid.uuid4().hex}
        )
    elif field == "host-schema":
        value["schema"] = 1
    elif field == "suite":
        value["checks"].pop()
    else:
        value["created_monotonic"] -= 86400
    with pytest.raises(SandboxUnavailable):
        validate_receipt(value, startup.guard, startup.worker, startup.adapter)


def test_new_lock_owner_cannot_reuse_container_receipt(startup):
    value = startup.run()
    startup.guard.close()
    with DeviceLock(startup.guard.directory, startup.guard.device_uuid) as replacement:
        startup.control.guard = replacement
        startup.journal.guard = replacement
        with pytest.raises(SandboxUnavailable, match="current owner"):
            validate_receipt(value, replacement, startup.worker, startup.adapter)


def test_external_occupancy_closes_heartbeat_without_stopping_unknown_work(startup):
    startup.run()
    startup.occupied = True
    before = list(startup.engine.calls)
    with pytest.raises(SandboxUnavailable, match="external GPU"):
        gpu_readiness.heartbeat_ready(
            startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
        )
    assert not startup.path.exists()
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")}
        for call in startup.engine.calls[len(before) :]
    )
    assert GPUDevice.objects.get(pk=startup.guard.device_uuid).state == "quarantined"


@pytest.mark.parametrize("retained_container_record", [False, True])
def test_pending_host_task_is_not_relabelled_by_failed_container_retries(
    startup, retained_container_record
):
    if not retained_container_record:
        startup.journal.recover()
    host = environment_fixture()
    original = {
        "owner_token": uuid.uuid4(),
        "worker_id": "previous-host",
        "environment": host,
        "fingerprint": canonical_digest(host),
    }
    GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(**original)
    for _ in range(2):
        with pytest.raises(SandboxUnavailable, match="previous host"):
            startup.run()
        row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
        assert all(getattr(row, key) == value for key, value in original.items())
        assert (row.job_id, row.reservation_token) == startup.slot
        if retained_container_record:
            assert startup.journal.load() is not None
            assert not startup.engine.state["State"]["Running"]
    assert not startup.tasks and not startup.path.exists()


@pytest.mark.parametrize("previous", ["recovered-host", "unpublished-container"])
def test_verified_backend_transition_and_unpublished_v3_upgrade_remain_recoverable(
    startup, previous
):
    fields = {"owner_token": uuid.uuid4(), "worker_id": "previous-controller"}
    if previous == "recovered-host":
        startup.journal.recover()
        gpu_resources.quarantine(startup.guard)
        gpu_resources.confirm_recovered(
            startup.guard, job_id=startup.job.id, reservation_token=startup.job.lease_token
        )
        host = environment_fixture()
        fields.update(environment=host, fingerprint=canonical_digest(host))
        startup.slot = None, None
    else:
        fields.update(environment={}, fingerprint="9" * 64)
    GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(**fields)
    startup.run()
    row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert row.state == "ready" and row.job_id is None
    assert row.owner_token == startup.guard.owner_token
    assert row.environment == startup.adapter.environment


def test_database_failure_stops_original_container_without_archiving_or_releasing(
    startup, monkeypatch
):
    def refuse(*_args, **_kwargs):
        raise DatabaseError("fixed database failure")

    monkeypatch.setattr(gpu_resources, "adopt", refuse)
    with pytest.raises(DatabaseError, match="database failure"):
        startup.run()
    assert startup.journal.load() is not None
    assert not startup.engine.state["State"]["Running"]
    assert GPUDevice.objects.get(pk=startup.guard.device_uuid).job_id == startup.job.id
    assert not startup.tasks


@pytest.mark.parametrize("defect", ["owner", "publication"])
def test_final_publication_failure_cannot_release_original_slot(startup, monkeypatch, defect):
    original = ReceiptStore._write

    def write(store, encoded):
        if defect == "publication":
            raise OSError("fixed publication failure")
        original(store, encoded)
        GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(owner_token=uuid.uuid4())

    monkeypatch.setattr(ReceiptStore, "_write", write)
    with pytest.raises(OSError if defect == "publication" else gpu_resources.DeviceOwnershipError):
        startup.run()
    row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert row.state == "quarantined" and row.job_id == startup.job.id
    assert not startup.path.exists()


def test_initial_container_recovery_alone_never_releases_the_slot(startup):
    value = recover_controller(startup.guard, startup.worker, startup.adapter)
    assert value["ready"] is False and value["recovered"] is True
    assert value["container_recovery"]["had_record"] is True
    assert startup.journal.load() is None and startup.engine.state is None
    assert not startup.path.exists()
    assert GPUDevice.objects.get(pk=startup.guard.device_uuid).job_id == startup.job.id


def test_claim_and_cancel_recovery_keep_current_receipt_and_exact_slot(startup, user, gpu_version):
    startup.run()
    queued(user, gpu_version, evaluation_fingerprint=startup.adapter.fingerprint)
    job = gpu_readiness.claim_ready(
        startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
    )
    assert job is not None and job.evaluation_fingerprint == startup.adapter.fingerprint
    gpu_resources.quarantine(startup.guard, job)
    startup.slot = job.id, job.lease_token
    startup.events.clear()
    gpu_readiness.recover_cancelled(
        startup.guard,
        startup.worker,
        startup.adapter,
        job,
        receipt_directory=startup.directory,
        on_tick=lambda: None,
    )
    row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert row.state == "ready" and row.job_id is None
    assert check_receipt(startup.directory, startup.guard, startup.worker, startup.adapter)


def test_database_owner_change_during_initial_cleanup_prevents_new_work(startup, monkeypatch):
    original = startup.journal.recover
    replacement = uuid.uuid4()

    def changed():
        value = original()
        GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(owner_token=replacement)
        return value

    monkeypatch.setattr(startup.journal, "recover", changed)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        startup.run()
    row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert row.owner_token == replacement and row.job_id == startup.job.id
    assert not startup.tasks and not startup.path.exists()
