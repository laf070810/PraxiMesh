"""Actual Django lifecycle/DB/locks with explicitly modeled GPU methods."""

import copy
from contextlib import contextmanager
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core import gpu_worker
from core.management.commands import container_gpu_worker as command
from core.models import GPUDevice, Submission
from judge.gpu_receipt import CHECKS
from tests.gpu_evidence_fixtures import result_fixture, version_fixture
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_readiness import startup as startup
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def worker(startup, configured, settings, monkeypatch):
    value = {
        "schema": 1,
        "enabled": True,
        "gpu": startup.guard.device_uuid,
        "worker_id": startup.worker,
        "task_image": startup.adapter.image,
        "driver_manifest": copy.deepcopy(startup.adapter.driver_manifest),
        "database": {"url": "postgresql://worker:synthetic-secret@db/test", "sslmode": "disable"},
    }
    settings.JUDGE_ENABLED = True
    settings.WORKER_CONFIGURATION = value
    monkeypatch.setattr(command, "read_config", lambda: copy.deepcopy(value))
    monkeypatch.setattr(command, "verify_image", lambda: None)
    monkeypatch.setattr(command, "check_database", lambda: None)
    monkeypatch.setattr(command, "STATE", startup.directory.parent)
    startup.entered = startup.closed = 0

    @contextmanager
    def controller(device):
        assert device == startup.guard.device_uuid
        startup.entered += 1
        try:
            yield startup.control
        finally:
            startup.guard.assert_held()
            startup.closed += 1

    monkeypatch.setattr(command, "ContainerController", controller)
    startup.config = value
    startup.task_hook = None
    original = configured.run

    def run(task, request, **kwargs):
        if task.service.purpose == "maintenance":
            return original(task, request, **kwargs)
        kwargs["on_begin"]()
        if startup.task_hook:
            startup.task_hook(task, request)
        kwargs["on_tick"]()
        task.cleanup = {"synthetic": True}
        return {
            "result": result_fixture(request["mode"]),
            "artifacts": {
                "image": startup.adapter.image,
                "task_contract": startup.adapter.environment["artifacts"]["task_contract"],
                "resource_profile": startup.adapter.environment["resource_profile"],
                "limits": copy.deepcopy(startup.adapter.environment["workload_limits"]),
            },
        }

    configured.run = run
    return startup


@pytest.mark.parametrize("with_job", [False, True])
def test_container_command_runs_full_startup_claim_and_shutdown(worker, user, with_job):
    job = (
        queued(user, version_fixture(), evaluation_fingerprint=worker.adapter.fingerprint)
        if with_job
        else None
    )
    call_command("container_gpu_worker", once=True)
    assert worker.events == list(CHECKS)
    assert worker.entered == worker.closed == 1
    assert worker.journal.load() is None and not worker.path.exists()
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id is None and device.checked_at is None
    if job:
        job.refresh_from_db()
        assert job.status == "finished" and job.verdict == "accepted"
        assert job.budget_state == "consumed"
    worker.guard.assert_held()


def test_cancelled_container_task_recovers_exact_slot_before_shutdown(worker, user):
    job = queued(user, version_fixture(), evaluation_fingerprint=worker.adapter.fingerprint)

    def cancel(task, _request):
        assert task.service.job_id == job.id
        worker.slot = job.id, task.service.reservation_token
        Submission.objects.filter(pk=job.id).update(lease_until=timezone.now())

    worker.task_hook = cancel
    call_command("container_gpu_worker", once=True)
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id is None
    job.refresh_from_db()
    assert job.verdict != "accepted" and job.budget_state == "consumed"
    assert worker.journal.load() is None and not worker.path.exists()


@pytest.mark.parametrize("stage", ["resources", "kernel/public-run", "controller/transport-loss"])
def test_startup_failure_retains_original_slot_and_sanitizes_errors(worker, stage, caplog):
    worker.failure = stage
    with pytest.raises(CommandError, match="stage=worker error=SandboxUnavailable"):
        call_command("container_gpu_worker", once=True)
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id == worker.job.id
    assert not worker.path.exists() and worker.closed == 1
    assert "synthetic-secret" not in caplog.text


def test_readonly_check_never_opens_controller_or_changes_database(worker):
    before = list(GPUDevice.objects.values())
    output = StringIO()
    call_command("container_gpu_worker", check=True, stdout=output)
    assert "GPU readiness was not checked" in output.getvalue()
    assert before == list(GPUDevice.objects.values())
    assert not worker.entered and not worker.events and not worker.path.exists()


@pytest.mark.parametrize("defect", ["disabled", "settings", "image", "database", "old-mounts"])
def test_container_command_rejects_configuration_without_startup(
    worker, settings, monkeypatch, defect
):
    if defect == "disabled":
        settings.JUDGE_ENABLED = False
    elif defect == "settings":
        settings.WORKER_CONFIGURATION = None
    elif defect in {"image", "database"}:

        def fail():
            raise ValueError("synthetic-private-credential")

        monkeypatch.setattr(
            command, "verify_image" if defect == "image" else "check_database", fail
        )
    else:
        worker.control.saved["version"] = 3
    with pytest.raises(CommandError) as error:
        call_command("container_gpu_worker", once=True)
    assert "synthetic-private-credential" not in str(error.value)
    assert not worker.events and not worker.path.exists()
    assert worker.entered == worker.closed == int(defect == "old-mounts")


def test_stop_during_startup_quarantines_and_releases_controller(worker, configured, monkeypatch):
    stop = gpu_worker.StopFlag()
    monkeypatch.setattr(command, "StopFlag", lambda: stop)
    original = configured.run

    def run(*args, **kwargs):
        value = original(*args, **kwargs)
        stop.requested = True
        return value

    configured.run = run
    call_command("container_gpu_worker", once=True)
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id == worker.job.id
    assert not worker.path.exists() and worker.closed == 1
