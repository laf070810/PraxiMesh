import io
import json
import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core.models import ProblemVersion, QueueLock, Submission, WorkerHealth
from core.queue import (
    AdmissionError,
    admission_state,
    available,
    begin_execution,
    claim,
    configured_fingerprint,
    enqueue,
    finish,
    pause_admission,
    resume_admission,
)


@pytest.fixture
def ready(catalog, settings):
    settings.JUDGE_ENABLED = True
    return WorkerHealth.objects.create(
        id="admission-test",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )


def test_pause_keeps_existing_work_and_resume_requires_owner(ready, user):
    version = ProblemVersion.objects.first()
    job = enqueue(user, version, "source", "submit")
    token = uuid.uuid4()
    pause_admission(token)
    paused_at = QueueLock.objects.get(pk=1).paused_at
    pause_admission(token)  # Safe retry does not replace the original pause time.
    assert QueueLock.objects.get(pk=1).paused_at == paused_at
    assert not available()
    with pytest.raises(AdmissionError):
        enqueue(user, version, "other source", "run")
    with pytest.raises(AdmissionError, match="Another deployment"):
        pause_admission(uuid.uuid4())
    with pytest.raises(AdmissionError, match="owning deployment"):
        resume_admission(uuid.uuid4())
    assert claim().pk == job.pk  # Pausing admission does not interrupt queued work.
    assert Submission.objects.count() == 1
    resume_admission(token)
    assert available()
    assert enqueue(user, version, "after resume", "run").evaluation_fingerprint == ready.fingerprint


@pytest.mark.parametrize(
    "problem",
    ["js-run-length", "sensor-analysis-report", "train-linear-model", "weighted-line-fit"],
)
def test_gvisor_does_not_advertise_or_admit_unsupported_languages(
    ready, settings, user, client, problem
):
    settings.JUDGE_BACKEND = "gvisor"
    ready.fingerprint = configured_fingerprint()
    ready.save()
    version = ProblemVersion.objects.get(problem_id=problem)
    assert available() and not available(version)
    assert not client.get(f"/problems/{problem}/").context["judge_available"]
    with pytest.raises(AdmissionError):
        enqueue(user, version, version.data["reference"], "submit")
    assert not Submission.objects.exists()


@pytest.mark.parametrize("failure", ["stale", "wrong_policy", "unready", "disabled"])
def test_resume_fails_closed_without_ready_matching_worker(ready, settings, failure):
    token = uuid.uuid4()
    pause_admission(token)
    if failure == "stale":
        ready.checked_at = timezone.now() - timedelta(minutes=1)
    elif failure == "wrong_policy":
        ready.fingerprint = "f" * 64
    elif failure == "unready":
        ready.ready = False
    else:
        settings.JUDGE_ENABLED = False
    ready.save()
    with pytest.raises(AdmissionError, match="fresh matching worker"):
        resume_admission(token)
    assert QueueLock.objects.get(pk=1).pause_token == token


def test_policy_fence_skips_legacy_and_other_policy_jobs(ready, user):
    version = ProblemVersion.objects.first()
    jobs = [
        Submission.objects.create(
            user=user,
            version=version,
            source="source",
            source_digest="a" * 64,
            mode="submit",
            evaluation_fingerprint=fingerprint,
        )
        for fingerprint in ["", "e" * 64, ready.fingerprint]
    ]
    assert claim().pk == jobs[2].pk
    assert claim() is None
    assert claim("e" * 64).pk == jobs[1].pk
    jobs[0].refresh_from_db()
    assert jobs[0].status == "queued"
    token = uuid.uuid4()
    pause_admission(token)
    with pytest.raises(AdmissionError, match="another evaluation policy"):
        resume_admission(token)
    state = admission_state()
    assert state["incompatible_pending"] == 2
    assert state["queued"] == 1 and state["running"] == 2


def test_drain_timeout_preserves_pause_and_jobs(ready, user):
    job = enqueue(user, ProblemVersion.objects.first(), "private source", "submit")
    token = uuid.uuid4()
    output = io.StringIO()
    with pytest.raises(CommandError, match="remains paused"):
        call_command("judge_admission", "drain", token=token, timeout=0, stdout=output)
    state = json.loads(output.getvalue())
    assert state["admission"] == "paused" and state["queued"] == 1
    assert "private source" not in output.getvalue()
    assert QueueLock.objects.get(pk=1).pause_token == token
    job.refresh_from_db()
    assert job.status == "queued" and job.verdict == ""
    claimed = claim()
    assert begin_execution(claimed)
    finish(claimed, {"verdict": "wrong_answer"}, {})
    output = io.StringIO()
    call_command("judge_admission", "drain", token=token, timeout=0, stdout=output)
    assert json.loads(output.getvalue())["running"] == 0
    assert not available()
    call_command("judge_admission", "resume", token=token, stdout=io.StringIO())
    assert available()


def test_interrupted_drain_does_not_reopen_or_cancel(ready, user, monkeypatch):
    from core.management.commands import judge_admission

    enqueue(user, ProblemVersion.objects.first(), "source", "submit")
    token = uuid.uuid4()

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(judge_admission.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        call_command("judge_admission", "drain", token=token)
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert Submission.objects.get().status == "queued"


def test_paused_state_keeps_reading_and_private_drafts(ready, user, logged_client):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    pause_admission(uuid.uuid4())
    assert logged_client.get("/health/").json()["evaluation"] == "unavailable"
    assert logged_client.get("/learn/").status_code == 200
    assert logged_client.get("/problems/sum-positive/").status_code == 200
    response = logged_client.post(
        "/problems/sum-positive/draft/", {"source": "private draft", "version": version.pk}
    )
    assert response.status_code == 302
    assert "private draft" in logged_client.get("/problems/sum-positive/").content.decode()
    response = logged_client.post(
        "/problems/sum-positive/submit/",
        {"source": "source", "version": version.pk, "mode": "submit"},
        HTTP_ACCEPT="application/json",
    )
    assert response.status_code == 503
    assert not Submission.objects.exists()


def test_admission_command_validates_operator_arguments(ready):
    for action in ["pause", "resume", "drain"]:
        with pytest.raises(CommandError, match="require --token"):
            call_command("judge_admission", action)
    for timeout in [-1, 3601, float("nan"), float("inf")]:
        with pytest.raises(CommandError, match="timeout"):
            call_command("judge_admission", "drain", token=uuid.uuid4(), timeout=timeout)
    assert not QueueLock.objects.filter(pause_token__isnull=False).exists()
