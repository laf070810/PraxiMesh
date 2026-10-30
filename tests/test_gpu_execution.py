"""Real database settlement ordering with a mocked adapter; not GPU evidence."""

import time
from datetime import timedelta

import pytest
from django.utils import timezone

from core import gpu_resources
from core.gpu_control import GPUJobControl
from core.gpu_execution import execute_claimed
from core.models import GPUDevice, Submission
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_service import OwnedJobService
from judge.gpu_session import GPUExecutionError
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_resources import DEVICE, FINGERPRINT, queued
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import guard as guard

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def execution(guard, gpu_version, user, monkeypatch):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
    adapter.fingerprint = FINGERPRINT
    events = []
    monkeypatch.setattr(OwnedJobService, "stop", lambda _self: events.append("stopped"))
    original_quarantine = GPUJobControl.quarantine

    def quarantine(control):
        events.append("quarantined")
        return original_quarantine(control)

    monkeypatch.setattr(GPUJobControl, "quarantine", quarantine)
    return job, adapter, events


def test_result_is_uncommitted_until_the_supervised_adapter_returns(guard, execution, monkeypatch):
    job, adapter, events = execution

    def evaluated(_self, service, **kwargs):
        assert service.job_id == job.id and service.reservation_token == job.lease_token
        assert time.monotonic() < kwargs["deadline"] <= time.monotonic() + 180
        kwargs["on_tick"]()
        kwargs["on_begin"]()
        job.refresh_from_db()
        assert job.status == "running" and job.budget_state == "consumed"
        assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id
        events.append("adapter-returned")
        return {"verdict": "wrong_answer"}, {"scope": "database unit test only"}

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", evaluated)
    assert execute_claimed(guard, job, adapter) == 1
    job.refresh_from_db()
    assert job.status == "finished" and job.verdict == "wrong_answer"
    assert job.budget_state == "consumed"
    assert GPUDevice.objects.get(pk=DEVICE).state == "ready"
    assert events == ["adapter-returned"]


@pytest.mark.parametrize("started", [False, True])
def test_failure_stops_before_quarantine_and_does_not_free_slot(
    guard, execution, monkeypatch, started
):
    job, adapter, events = execution

    def failed(_self, _guard, **kwargs):
        if started:
            kwargs["on_begin"]()
        raise SandboxUnavailable("fixed adapter failure")

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", failed)
    with pytest.raises(SandboxUnavailable, match="adapter failure"):
        execute_claimed(guard, job, adapter)
    job.refresh_from_db()
    assert job.status == "running" and job.budget_state == ("consumed" if started else "reserved")
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "quarantined" and device.job_id == job.id
    assert events == ["stopped", "quarantined"]


def test_account_deletion_during_execution_retains_physical_reservation(
    guard, execution, monkeypatch
):
    job, adapter, events = execution

    def deleted(_self, _guard, **kwargs):
        kwargs["on_begin"]()
        job.user.delete()
        kwargs["on_tick"]()
        pytest.fail("A deleted user's task must not continue")

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", deleted)
    with pytest.raises(SandboxUnavailable):
        execute_claimed(guard, job, adapter)
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "quarantined" and device.job_id == job.id
    assert not Submission.objects.filter(pk=job.id).exists()
    assert events == ["stopped", "quarantined"]


def test_expired_lease_after_physical_completion_cannot_write_a_verdict(
    guard, execution, monkeypatch
):
    job, adapter, events = execution

    def expired(_self, _guard, **kwargs):
        kwargs["on_begin"]()
        Submission.objects.filter(pk=job.id).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        return {"verdict": "wrong_answer"}, {"scope": "database unit test only"}

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", expired)
    assert execute_claimed(guard, job, adapter) == 0
    job.refresh_from_db()
    assert job.verdict == "internal_error" and job.status == "finished"
    # The adapter's physical completion is assumed only for this DB test;
    # lease loss still cannot preserve the candidate learner result.
    assert GPUDevice.objects.get(pk=DEVICE).state == "ready"
    assert not events


def test_task_fingerprint_mismatch_stops_and_quarantines_without_execution(
    guard, execution, monkeypatch
):
    job, adapter, events = execution
    adapter.fingerprint = "f" * 64
    monkeypatch.setattr(
        GPUExecutionAdapter, "evaluate", lambda *_args, **_kwargs: pytest.fail("Must not execute")
    )
    with pytest.raises(SandboxUnavailable, match="fingerprint"):
        execute_claimed(guard, job, adapter)
    job.refresh_from_db()
    assert job.budget_state == "reserved"
    assert events == ["stopped", "quarantined"]


def test_cleanup_failure_does_not_block_on_database(guard, execution, monkeypatch):
    job, adapter, events = execution

    def failed(*_args, **_kwargs):
        raise SandboxUnavailable("fixed failure")

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", failed)
    monkeypatch.setattr(OwnedJobService, "stop", failed)
    with pytest.raises(SandboxUnavailable):
        execute_claimed(guard, job, adapter)
    assert not events
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_expired_slot_never_starts_adapter(guard, execution, monkeypatch):
    job, adapter, _events = execution
    job.started_at = timezone.now() - timedelta(seconds=181)
    monkeypatch.setattr(
        GPUExecutionAdapter, "evaluate", lambda *_args, **_kwargs: pytest.fail("Must not execute")
    )
    with pytest.raises(GPUExecutionError):
        execute_claimed(guard, job, adapter)
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id


def test_error_cleanup_preserves_the_observed_service_instance(guard, execution, monkeypatch):
    job, adapter, events = execution

    def changed(_self, service, **_kwargs):
        def refuse_replacement():
            events.append("replacement-refused")
            raise SandboxUnavailable("fixed changed invocation")

        # Represents a service object which has already observed a particular
        # invocation. Reconstructing it by name would discard that fence.
        service.stop = refuse_replacement
        raise SandboxUnavailable("fixed adapter failure")

    monkeypatch.setattr(GPUExecutionAdapter, "evaluate", changed)
    with pytest.raises(SandboxUnavailable, match="changed invocation"):
        execute_claimed(guard, job, adapter)
    assert events == ["replacement-refused"]
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id
