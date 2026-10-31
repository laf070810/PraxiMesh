"""Real DB transitions with modeled container execution; no hardware evidence."""

import copy
from datetime import timedelta

import pytest
from django.utils import timezone

from core import gpu_resources
from core.gpu_admission import valid_environment
from core.gpu_control import GPUJobControl, GPUJobLost
from core.gpu_evidence import environment_profile
from core.gpu_execution import execute_claimed
from core.models import GPUDevice, Submission
from core.result_evidence import InvalidResult, safe_result, verified_evidence
from judge.backends import result_environment_label
from judge.gpu_execution_contract import canonical_digest
from judge.sandbox import SandboxUnavailable
from learning.evidence import build_evidence
from tests.gpu_evidence_fixtures import job_fixture, version_fixture
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_gpu_resources import guard as guard
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def execution(guard, configured, user, monkeypatch):
    configured.control.guard = guard
    GPUDevice.objects.filter(pk=guard.device_uuid).update(
        fingerprint=configured.adapter.fingerprint
    )
    version = version_fixture("gpu-container-settlement-fixture")
    queued(user, version, evaluation_fingerprint=configured.adapter.fingerprint)
    job = gpu_resources.claim(guard)
    original = GPUJobControl.quarantine

    def quarantine(value):
        configured.events.append("db-quarantine")
        return original(value)

    monkeypatch.setattr(GPUJobControl, "quarantine", quarantine)
    return job, configured


def test_container_completion_uses_existing_private_formal_evidence(guard, execution, other_user):
    job, state = execution
    original = state.run

    def observed(task, request, **kwargs):
        assert task.service.job_id == job.id and task.service.reservation_token == job.lease_token
        result = original(task, request, **kwargs)
        job.refresh_from_db()
        assert job.status == "running" and job.budget_state == "consumed"
        assert GPUDevice.objects.get(pk=guard.device_uuid).job_id == job.id
        return result

    state.run = observed
    assert execute_claimed(guard, job, state.adapter) == 1
    job.refresh_from_db()
    assert job.status == "finished" and job.verdict == "accepted"
    assert job.environment["backend"] == "gvisor-gpu-container"
    assert verified_evidence(job)["verdict"] == "accepted"
    assert safe_result(job)["performance"]["sample_batches"] == 5
    assert build_evidence(job.user)["summary"]["completed"] == 1
    assert build_evidence(other_user)["summary"]["completed"] == 0
    device = GPUDevice.objects.get(pk=guard.device_uuid)
    assert device.state == "ready" and device.job_id is None
    assert "db-quarantine" not in state.events and len(state.tasks) == 1


@pytest.mark.parametrize("started", [False, True])
def test_container_failure_cleans_original_task_before_database_quarantine(
    guard, execution, started
):
    job, state = execution

    def failed(task, request, **kwargs):
        if started:
            kwargs["on_begin"]()
        raise SandboxUnavailable("fixed container failure")

    state.run = failed
    with pytest.raises(SandboxUnavailable, match="container failure"):
        execute_claimed(guard, job, state.adapter)
    job.refresh_from_db()
    assert job.budget_state == ("consumed" if started else "reserved")
    device = GPUDevice.objects.get(pk=guard.device_uuid)
    assert device.state == "quarantined" and device.job_id == job.id
    assert state.events.index("original-cleanup") < state.events.index("db-quarantine")


def test_cleanup_error_keeps_same_task_and_does_not_wait_on_database(guard, execution):
    job, state = execution

    def failed(task, request, **kwargs):
        def refuse():
            state.events.append("replacement-refused")
            raise SandboxUnavailable("Original container identity changed")

        task.recover = refuse
        raise SandboxUnavailable("fixed execution failure")

    state.run = failed
    with pytest.raises(SandboxUnavailable, match="identity changed"):
        execute_claimed(guard, job, state.adapter)
    assert len(state.tasks) == 1 and state.events[-1] == "replacement-refused"
    assert "db-quarantine" not in state.events
    assert GPUDevice.objects.get(pk=guard.device_uuid).job_id == job.id


@pytest.mark.parametrize("lost", ["account", "lease"])
def test_lost_lease_or_deleted_account_withholds_container_result(guard, execution, lost):
    job, state = execution
    original = state.run

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if lost == "account":
            job.user.delete()
        else:
            Submission.objects.filter(pk=job.id).update(
                lease_until=timezone.now() - timedelta(seconds=1)
            )
        return value

    state.run = changed
    with pytest.raises(GPUJobLost):
        execute_claimed(guard, job, state.adapter)
    assert GPUDevice.objects.get(pk=guard.device_uuid).job_id == job.id
    assert not Submission.objects.filter(pk=job.id, verdict="accepted").exists()
    assert state.events.index("original-cleanup") < state.events.index("db-quarantine")


@pytest.mark.parametrize("stage", ["fingerprint", "initial-check", "configuration"])
def test_preparation_failure_does_not_create_a_container(guard, execution, stage):
    job, state = execution
    if stage == "fingerprint":
        job.evaluation_fingerprint = "f" * 64
    elif stage == "configuration":
        state.engine["ServerVersion"] = "29.9.3"

    def check():
        if stage == "initial-check":
            raise SandboxUnavailable("fixed receipt rejection")

    with pytest.raises(SandboxUnavailable):
        execute_claimed(guard, job, state.adapter, on_check=check)
    assert not state.tasks
    assert GPUDevice.objects.get(pk=guard.device_uuid).state == "quarantined"


def test_container_history_is_valid_without_opening_admission(configured, user, settings):
    version = version_fixture()
    job = job_fixture(user, version)
    env = copy.deepcopy(configured.adapter.environment)
    env["execution_digest"] = "0" * 64
    job.evaluation_fingerprint = canonical_digest(env)
    job.environment = {**env, "language": "cuda", "evaluator": "cuda-kernel-v1"}
    job.save()
    assert verified_evidence(job)["verdict"] == "accepted"
    profile = environment_profile(job.environment, job.evaluation_fingerprint)
    assert profile["id"] == "cuda-session-v1"
    label = result_environment_label(job.environment, job.evaluation_fingerprint)
    assert env["target"]["model"] in label and job.evaluation_fingerprint in label
    device = GPUDevice(
        id=env["target"]["uuid"], environment=env, fingerprint=job.evaluation_fingerprint
    )
    assert not valid_environment(device, policy=env["execution_digest"])
    # Keeping a valid historical record never publishes a current GPU device.
    assert not GPUDevice.objects.exists()
    job.environment["artifacts"]["task_image"] = "sha256:" + "f" * 64
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert safe_result(job) == {}
    assert result_environment_label(job.environment, job.evaluation_fingerprint) == ""
