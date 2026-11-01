"""Real isolated database transactions; GPU/worker publication is explicitly synthetic."""

import copy
import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core import deployment_gpu_check as module
from core.deployment_gpu_check import CandidateProbe
from core.management.commands.judge_admission import database_action
from core.models import (
    Draft,
    GPUDevice,
    Problem,
    ProblemVersion,
    QueueLock,
    Submission,
    WorkerHealth,
)
from core.queue import configured_fingerprint
from judge.gpu_contract import compile_request, validate_problem
from judge.gpu_execution_contract import canonical_digest
from judge.results import LABELS
from tests.gpu_evidence_fixtures import result_fixture
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle

pytestmark = pytest.mark.django_db


@pytest.fixture
def candidate(configured, settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_WASI_SHA256 = "b" * 64
    environment = copy.deepcopy(configured.adapter.environment)
    device = GPUDevice.objects.create(
        pk=environment["target"]["uuid"],
        worker_id="candidate-gpu",
        owner_token=uuid.uuid4(),
        fingerprint=canonical_digest(environment),
        environment=environment,
        state="ready",
        checked_at=timezone.now(),
    )
    settings.JUDGE_GPU_DEVICES = (device.pk,)
    cpu = WorkerHealth.objects.create(
        pk="candidate-cpu",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    token = uuid.uuid4()
    QueueLock.objects.create(pk=1, pause_token=token, paused_at=timezone.now())
    kwargs = {
        "token": token,
        "worker": device.worker_id,
        "cpu_worker": cpu.pk,
        "device": device.pk,
        "owner": device.owner_token,
        "fingerprint": device.fingerprint,
        "started": (timezone.now() - timedelta(seconds=10)).isoformat(),
    }
    return SimpleNamespace(
        device=device, cpu=cpu, token=token, kwargs=kwargs, probe=lambda: CandidateProbe(**kwargs)
    )


def prepare(candidate):
    return database_action(candidate.probe().prepare, 5)


def records():
    return list(Submission.objects.order_by("id").values())


def settle(candidate, *, clear=True):
    """Modeled worker settlement only; no CUDA or receipt is executed/created."""
    device = candidate.device
    profile = device.environment["session_profile"]
    now = timezone.now()
    for job in Submission.objects.filter(user__username=candidate.probe().name):
        verdict = "accepted" if job.source == module.CORRECT else "wrong_answer"
        result = result_fixture(verdict=verdict)
        if verdict == "wrong_answer":
            result["passed"] = 0
            result["public_cases"] = [
                {"name": "public", "verdict": verdict, "label": LABELS[verdict]}
            ]
        else:
            result["performance"].update(
                profile=profile["id"],
                profile_digest=canonical_digest(profile),
                measurement=profile["measurement"],
                warmup_batches=profile["warmup_batches"],
                sample_batches=profile["sample_batches"],
                batch_repetitions=profile["batch_repetitions"],
            )
        Submission.objects.filter(pk=job.pk).update(
            status="finished",
            verdict=verdict,
            result=result,
            environment=device.environment | {"language": "cuda", "evaluator": "cuda-kernel-v1"},
            started_at=job.created_at,
            execution_started_at=job.created_at,
            finished_at=now,
            lease_token=uuid.uuid4(),
            budget_state="consumed",
        )
    if clear:
        GPUDevice.objects.filter(pk=device.pk).update(
            state="ready",
            checked_at=now,
            job_id=None,
            reservation_token=None,
        )


def test_fixed_content_and_programs_are_data_not_host_execution(candidate):
    data = candidate.probe().data
    validate_problem(data)
    for source in module.SOURCES:
        compile_request(source, data["operation"])
    assert data["reference"] == module.CORRECT
    assert [len(row["expected"]) for row in data["tests"]] == [3, 257]


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "wrong_device",
        "backend",
        "owner",
        "worker",
        "fingerprint",
        "open",
        "other_pause",
        "stale",
        "future",
        "unready",
        "cpu_missing",
        "host_environment",
    ],
)
def test_preparation_rejects_wrong_identity_or_unready_state_without_writes(
    candidate, settings, change
):
    if change == "disabled":
        settings.JUDGE_ENABLED = False
    elif change == "wrong_device":
        settings.JUDGE_GPU_DEVICES = ()
    elif change == "backend":
        settings.JUDGE_BACKEND = "gvisor"
    elif change == "owner":
        candidate.kwargs["owner"] = uuid.uuid4()
    elif change == "worker":
        candidate.kwargs["worker"] = "another-gpu"
    elif change == "fingerprint":
        candidate.kwargs["fingerprint"] = "a" * 64
    elif change == "open":
        QueueLock.objects.update(pause_token=None, paused_at=None)
    elif change == "other_pause":
        QueueLock.objects.update(pause_token=uuid.uuid4())
    elif change in {"stale", "future"}:
        GPUDevice.objects.update(
            checked_at=timezone.now() + timedelta(seconds=60 if change == "future" else -60)
        )
    elif change == "unready":
        GPUDevice.objects.update(state="quarantined", checked_at=None)
    elif change == "cpu_missing":
        candidate.cpu.delete()
    else:
        environment = copy.deepcopy(candidate.device.environment)
        environment["backend"] = "gvisor-gpu"
        GPUDevice.objects.update(environment=environment)
    before = QueueLock.objects.values().get(pk=1)
    with pytest.raises(CommandError):
        candidate.probe().run(timeout=0)
    assert not Submission.objects.exists() and not get_user_model().objects.exists()
    assert not Problem.objects.exists() and not ProblemVersion.objects.exists()
    assert QueueLock.objects.values().get(pk=1) == before


def test_timeout_and_retry_retain_exact_submissions_and_pause(candidate):
    original = QueueLock.objects.values().get(pk=1)
    with pytest.raises(CommandError, match="timed out"):
        candidate.probe().run(timeout=0)
    before = records()
    assert len(before) == 2 and {row["source"] for row in before} == set(module.SOURCES)
    with pytest.raises(CommandError, match="timed out"):
        candidate.probe().run(timeout=0)
    assert records() == before
    assert QueueLock.objects.values().get(pk=1) == original


def test_success_retains_evidence_until_explicit_cleanup_of_only_owned_fixtures(candidate, user):
    prepare(candidate)
    settle(candidate)
    original = QueueLock.objects.values().get(pk=1)
    result = candidate.probe().run(timeout=0)
    assert result["passed"] is True and result["verdicts"] == list(module.VERDICTS)
    assert result["cleanup_passed"] is False and result["owner_token"] == str(
        candidate.device.owner_token
    )
    assert Submission.objects.count() == 2
    assert candidate.probe().run(cleanup=True)["cleanup_passed"] is True
    assert not Submission.objects.exists() and not Problem.objects.exists()
    assert not ProblemVersion.objects.exists()
    assert list(get_user_model().objects.values_list("pk", flat=True)) == [user.pk]
    assert QueueLock.objects.values().get(pk=1) == original


def test_finished_jobs_do_not_allow_deleting_an_unrecovered_physical_reservation(candidate):
    jobs = prepare(candidate)
    GPUDevice.objects.update(
        state="quarantined", checked_at=None, job_id=jobs[0].pk, reservation_token=uuid.uuid4()
    )
    settle(candidate, clear=False)
    before = records()
    slot = GPUDevice.objects.values().get(pk=candidate.device.pk)
    with pytest.raises(CommandError, match="timed out"):
        candidate.probe().run(timeout=0)
    with pytest.raises(CommandError, match="physical recovery"):
        candidate.probe().run(cleanup=True)
    assert records() == before and GPUDevice.objects.values().get(pk=candidate.device.pk) == slot
    settle(candidate)
    assert candidate.probe().run(timeout=0)["passed"] is True


@pytest.mark.parametrize(
    "damage",
    [
        "verdict",
        "performance",
        "source",
        "version",
        "lease",
        "environment",
        "budget",
        "time",
        "related_draft",
        "missing_job",
    ],
)
def test_bad_or_unowned_evidence_is_retained(candidate, damage):
    jobs = prepare(candidate)
    settle(candidate)
    job = Submission.objects.get(pk=jobs[0].pk)
    if damage == "verdict":
        job.verdict = "wrong_answer"
    elif damage == "performance":
        job.result.pop("performance")
    elif damage == "source":
        job.source += "// changed"
    elif damage == "version":
        job.version.data["summary"] = "Unrecognized content"
        job.version.save()
    elif damage == "lease":
        job.lease_token = None
    elif damage == "environment":
        job.environment["target"]["model"] = "Another model"
    elif damage == "budget":
        job.budget_state = "reserved"
        job.execution_started_at = None
    elif damage == "time":
        candidate.kwargs["started"] = timezone.now().isoformat()
        GPUDevice.objects.update(checked_at=timezone.now())
    elif damage == "related_draft":
        Draft.objects.create(user=job.user, version=job.version, source="unrelated user work")
    elif damage == "missing_job":
        jobs[1].delete()
    job.save()
    before = records()
    with pytest.raises(CommandError):
        candidate.probe().run(timeout=0)
    assert (
        records() == before
        and get_user_model().objects.filter(username=candidate.probe().name).exists()
    )


def test_failure_cleanup_never_claims_success_and_does_not_require_cpu_health(candidate):
    prepare(candidate)
    settle(candidate)
    Submission.objects.update(verdict="internal_error", result={})
    candidate.cpu.delete()
    report = candidate.probe().run(cleanup=True)
    assert (
        report["passed"] is False and report["verdicts"] == [] and report["cleanup_passed"] is True
    )
    assert not Submission.objects.exists() and not Problem.objects.exists()


def test_cleanup_after_original_worker_recovery_uses_new_owner_but_original_fixtures(candidate):
    prepare(candidate)
    settle(candidate)
    candidate.kwargs["owner"] = uuid.uuid4()
    candidate.kwargs["started"] = timezone.now().isoformat()
    GPUDevice.objects.update(owner_token=candidate.kwargs["owner"], checked_at=timezone.now())
    assert candidate.probe().run(cleanup=True)["passed"] is False


def test_unrelated_or_orphan_reservation_blocks_cleanup(candidate):
    prepare(candidate)
    settle(candidate)
    extra = copy.copy(candidate.device)
    extra.pk = "GPU-" + str(uuid.uuid4())
    extra.state, extra.checked_at = "quarantined", None
    extra.job_id, extra.reservation_token = uuid.uuid4(), uuid.uuid4()
    extra.save(force_insert=True)
    before = records()
    with pytest.raises(CommandError, match="physical recovery"):
        candidate.probe().run(cleanup=True)
    assert records() == before and GPUDevice.objects.get(pk=extra.pk).job_id == extra.job_id


def test_second_enqueue_failure_rolls_back_temporary_gate_and_all_fixture_writes(
    candidate, monkeypatch
):
    original = module.enqueue
    calls = []

    def enqueue(*args):
        assert QueueLock.objects.get(pk=1).pause_token is None
        calls.append(args)
        if len(calls) == 2:
            raise module.AdmissionError("fixed second enqueue failure")
        return original(*args)

    monkeypatch.setattr(module, "enqueue", enqueue)
    before = QueueLock.objects.values().get(pk=1)
    with pytest.raises(CommandError, match="second enqueue"):
        candidate.probe().run(timeout=0)
    assert QueueLock.objects.values().get(pk=1) == before
    assert not Submission.objects.exists() and not get_user_model().objects.exists()


def test_elapsed_original_submission_budget_is_not_renewed_on_retry(candidate, monkeypatch):
    jobs = prepare(candidate)
    Submission.objects.update(
        created_at=timezone.now() - timedelta(seconds=module.WAIT_SECONDS + 1)
    )
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: pytest.fail("Expired original wait was renewed")
    )
    with pytest.raises(CommandError, match="timed out"):
        candidate.probe().run()
    assert set(Submission.objects.values_list("pk", flat=True)) == {job.pk for job in jobs}


def test_management_command_only_reports_completed_database_verification(candidate):
    arguments = {
        "token": candidate.token,
        "worker_id": candidate.kwargs["worker"],
        "cpu_worker_id": candidate.kwargs["cpu_worker"],
        "device": candidate.device.pk,
        "owner_token": candidate.kwargs["owner"],
        "fingerprint": candidate.kwargs["fingerprint"],
        "started_at": candidate.kwargs["started"],
        "timeout": 0,
    }
    output = io.StringIO()
    with pytest.raises(CommandError, match="timed out"):
        call_command("deployment_gpu_check", "check", stdout=output, **arguments)
    assert output.getvalue() == ""
    settle(candidate)
    call_command("deployment_gpu_check", "check", stdout=output, **arguments)
    assert json.loads(output.getvalue())["protocol"] == module.PROTOCOL


def test_completed_results_cannot_renew_expired_original_observation_budget(candidate):
    prepare(candidate)
    settle(candidate)
    candidate.kwargs["started"] = (
        timezone.now() - timedelta(seconds=module.WAIT_SECONDS + 60)
    ).isoformat()
    Submission.objects.update(
        created_at=timezone.now() - timedelta(seconds=module.WAIT_SECONDS + 1)
    )
    before = records()
    with pytest.raises(CommandError, match="timed out"):
        candidate.probe().run()
    assert records() == before


def test_cleanup_requires_explicit_terminal_status(candidate):
    prepare(candidate)
    settle(candidate)
    Submission.objects.update(status="unknown-state")
    before = records()
    with pytest.raises(CommandError, match="terminal"):
        candidate.probe().run(cleanup=True)
    assert records() == before


def test_invalid_environment_shape_fails_as_a_command_error_without_writes(candidate):
    GPUDevice.objects.update(environment=["invalid metadata"])
    with pytest.raises(CommandError, match="environment"):
        candidate.probe().run(timeout=0)
    assert not Submission.objects.exists()


def test_lost_verification_acknowledgement_keeps_original_jobs_for_retry(candidate):
    jobs = prepare(candidate)
    settle(candidate)
    original = {job.pk for job in jobs}
    candidate.probe().run(timeout=0)  # Its result can be lost before the coordinator journals it.
    assert set(Submission.objects.values_list("pk", flat=True)) == original
    assert candidate.probe().run(timeout=0)["cleanup_passed"] is False
    assert set(Submission.objects.values_list("pk", flat=True)) == original
