"""Real DB/lock fencing with explicit runtime doubles, never GPU acceptance."""

import copy
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from core import gpu_readiness, gpu_resources
from core.gpu_readiness import heartbeat_ready, initialize_controller
from core.models import GPUDevice
from judge.gpu_device_lock import DeviceLock
from judge.gpu_kernel_checks import check_request
from judge.gpu_receipt import CHECKS, ReceiptStore, check_receipt
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_isolation import REQUEST as RESOURCE_REQUEST
from tests.test_gpu_isolation import resource_report
from tests.test_gpu_kernel_checks import check_report
from tests.test_gpu_receipt import adapter_for
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def startup(tmp_path, monkeypatch, user, gpu_version):
    adapter = adapter_for(tmp_path)
    worker = "startup-unit-test"
    with DeviceLock(tmp_path / "locks", adapter.target["uuid"]) as guard:
        gpu_resources.adopt(guard, worker, adapter.fingerprint)
        # Prior operational state is synthetic. No earlier GPU validation is
        # claimed and every runtime operation below is an explicit test double.
        gpu_resources.confirm_recovered(guard, job_id=None, reservation_token=None)
        queued(user, gpu_version, evaluation_fingerprint=adapter.fingerprint)
        job = gpu_resources.claim(guard)
        directory = tmp_path / "receipts"
        path = directory / (guard.device_uuid + ".json")
        events = []
        services = []
        control = SimpleNamespace(failure=None, resource_defect=None)
        monkeypatch.setattr(OwnedJobService, "inspect", lambda _self: None)
        monkeypatch.setattr(OwnedJobService, "populated", lambda _self: False)
        monkeypatch.setattr(
            OwnedJobService,
            "stop",
            lambda self: {"unit": self.unit, "stopped": True, "cgroup_empty": True},
        )
        monkeypatch.setattr(gpu_readiness, "identify", lambda _uuid: copy.deepcopy(adapter.target))
        monkeypatch.setattr(gpu_readiness.DeviceMonitor, "observe", lambda _self: [])

        def phase(name, service, deadline, on_tick):
            assert service.purpose == "maintenance"
            assert 0 < deadline - time.monotonic() <= 180
            on_tick()
            device = GPUDevice.objects.get(pk=guard.device_uuid)
            assert device.state == "quarantined" and device.checked_at is None
            assert (device.job_id, device.reservation_token) == (job.id, job.lease_token)
            if name != "initial-recovery":
                assert not path.exists()
            events.append(name)
            services.append(service)
            if control.failure == name:
                raise SandboxUnavailable("fixed startup stage failure")

        def recover(service, *, deadline, on_tick):
            phase("initial-recovery", service, deadline, on_tick)
            return copy.deepcopy(adapter.environment)

        def resources(service, *, deadline, on_tick):
            phase("resources", service, deadline, on_tick)
            report = resource_report(RESOURCE_REQUEST, adapter.target)
            if control.resource_defect == "missing":
                report["checks"].pop()
            elif control.resource_defect == "cpu":
                report["gpu"] = None
            elif control.resource_defect == "duplicate":
                report["cleanup"][1]["id"] = report["cleanup"][0]["id"]
            return report, copy.deepcopy(adapter.environment)

        def kernel(service, name, *, deadline, on_tick):
            phase("kernel/" + name, service, deadline, on_tick)
            return check_report(check_request(name, uuid.uuid4().hex)), copy.deepcopy(
                adapter.environment
            )

        def controller(service, name, *, deadline, on_tick):
            phase("controller/" + name, service, deadline, on_tick)
            return {
                "name": name,
                "challenge": uuid.uuid4().hex,
                "disconnect_seconds": 20.0,
                "independent_stop": name != "transport-loss",
                "recovered": True,
            }, copy.deepcopy(adapter.environment)

        monkeypatch.setattr(adapter, "recover", recover)
        monkeypatch.setattr(adapter, "check_resources", resources)
        monkeypatch.setattr(adapter, "check_kernel", kernel)
        monkeypatch.setattr(adapter, "check_controller", controller)

        def run(**kwargs):
            return initialize_controller(
                guard, worker, adapter, receipt_directory=directory, **kwargs
            )

        yield SimpleNamespace(
            guard=guard,
            worker=worker,
            adapter=adapter,
            directory=directory,
            path=path,
            job=job,
            events=events,
            services=services,
            control=control,
            run=run,
        )


def test_full_sequence_preserves_old_slot_until_private_receipt_is_verified(startup):
    value = startup.run()
    assert startup.events == list(CHECKS)
    assert len({id(service) for service in startup.services}) == len(CHECKS)
    assert len({service.unit for service in startup.services}) == 1
    assert check_receipt(startup.directory, startup.guard, startup.worker, startup.adapter) == value
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "ready" and device.checked_at is not None
    assert device.environment == startup.adapter.environment
    assert device.job_id is None and device.reservation_token is None
    startup.job.refresh_from_db()
    assert startup.job.status == "finished" and startup.job.verdict == "internal_error"
    assert startup.job.budget_state == "released"


@pytest.mark.parametrize(
    "stage",
    [
        "initial-recovery",
        "resources",
        "kernel/vector-reference",
        "kernel/attach-death",
        "controller/service-deadline",
    ],
)
def test_any_incomplete_stage_retains_old_slot_and_cannot_issue_receipt(startup, stage):
    startup.control.failure = stage
    with pytest.raises(SandboxUnavailable, match="stage failure"):
        startup.run()
    assert startup.events[-1] == stage and not startup.path.exists()
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "quarantined" and device.checked_at is None
    assert device.environment == {}
    assert (device.job_id, device.reservation_token) == (startup.job.id, startup.job.lease_token)
    startup.job.refresh_from_db()
    assert startup.job.status == "running" and startup.job.budget_state == "reserved"


@pytest.mark.parametrize("defect", ["missing", "cpu", "duplicate"])
def test_partial_resource_report_cannot_complete_startup(startup, defect):
    startup.control.resource_defect = defect
    with pytest.raises(SandboxUnavailable):
        startup.run()
    assert not startup.path.exists()


@pytest.mark.parametrize("stage", ["kernel", "controller"])
def test_incomplete_kernel_or_controller_reports_cannot_issue_receipt(startup, monkeypatch, stage):
    name = "check_" + stage
    original = getattr(startup.adapter, name)

    def incomplete(*args, **kwargs):
        report, environment = original(*args, **kwargs)
        report.pop("cleanup" if stage == "kernel" else "recovered")
        return report, environment

    monkeypatch.setattr(startup.adapter, name, incomplete)
    with pytest.raises(SandboxUnavailable):
        startup.run()
    assert not startup.path.exists()


def test_owner_change_after_publication_cannot_release_old_slot(startup, monkeypatch):
    original = ReceiptStore._write
    replacement = uuid.uuid4()

    def changed(store, encoded):
        original(store, encoded)
        GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(owner_token=replacement)

    monkeypatch.setattr(ReceiptStore, "_write", changed)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        startup.run()
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.owner_token == replacement and device.state == "quarantined"
    assert device.job_id == startup.job.id
    assert not startup.path.exists()


def test_receipt_publication_failure_does_not_clear_reservation(startup, monkeypatch):
    def failed(_store, _encoded):
        raise OSError("fixed publication failure")

    monkeypatch.setattr(ReceiptStore, "_write", failed)
    with pytest.raises(OSError, match="publication failure"):
        startup.run()
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id == startup.job.id


def test_mismatched_fingerprint_during_checks_is_rejected(startup):
    def progress(name):
        if name == "resources":
            GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(fingerprint="f" * 64)

    with pytest.raises(gpu_resources.DeviceOwnershipError, match="identity changed"):
        startup.run(on_progress=progress)
    assert startup.events == ["initial-recovery", "resources"]
    assert not startup.path.exists()


def test_idle_heartbeat_requires_valid_receipt_and_idle_device(startup, monkeypatch):
    startup.run()
    assert heartbeat_ready(
        startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
    )

    def occupied(_self):
        raise SandboxUnavailable("fixed external GPU process")

    monkeypatch.setattr(gpu_readiness.DeviceMonitor, "observe", occupied)
    with pytest.raises(SandboxUnavailable, match="external GPU"):
        heartbeat_ready(
            startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
        )
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "quarantined" and device.checked_at is None
    assert not startup.path.exists()


def test_expired_receipt_closes_heartbeat_before_device_query(startup, monkeypatch):
    value = startup.run()
    value["created_monotonic"] -= 86400
    startup.path.write_text(json.dumps(value))
    monkeypatch.setattr(
        gpu_readiness, "identify", lambda _uuid: pytest.fail("must reject before querying GPU")
    )
    with pytest.raises(SandboxUnavailable, match="expired"):
        heartbeat_ready(
            startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
        )
    assert GPUDevice.objects.get(pk=startup.guard.device_uuid).state == "quarantined"
    assert not startup.path.exists()


def test_restart_runs_recovery_even_if_a_previous_document_is_present(startup, monkeypatch):
    startup.directory.mkdir(mode=0o700)
    startup.path.write_text('{"owner_token":"old-owner","checks":[]}')
    startup.path.chmod(0o600)
    startup.control.failure = "resources"
    with pytest.raises(SandboxUnavailable, match="stage failure"):
        startup.run()
    assert startup.events == ["initial-recovery", "resources"]
    assert not startup.path.exists()
