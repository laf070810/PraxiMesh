"""Administrative safety tests; successful real judging is a container E2E gate."""

import hashlib
import io
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.utils import timezone

from core.content import digest
from core.management.commands.deployment_check import (
    CORRECT,
    WRONG,
    fixture_identifier,
    smoke_problem,
)
from core.models import Draft, Problem, ProblemVersion, QueueLock, Submission, WorkerHealth
from core.queue import claim, configured_fingerprint


@pytest.fixture
def candidate(db, settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_WASI_SHA256 = "b" * 64
    token = uuid.uuid4()
    QueueLock.objects.create(pk=1, pause_token=token, paused_at=timezone.now())
    worker = WorkerHealth.objects.create(
        pk="candidate-check",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    return token, worker


@pytest.mark.parametrize(
    "failure",
    ["open", "other_token", "missing_worker", "stale", "future", "wrong_policy", "unready"],
)
def test_candidate_preconditions_fail_closed_without_creating_records(candidate, failure):
    token, worker = candidate
    if failure == "open":
        QueueLock.objects.update(pause_token=None, paused_at=None)
    elif failure == "other_token":
        token = uuid.uuid4()
    elif failure == "missing_worker":
        worker.delete()
    elif failure == "stale":
        WorkerHealth.objects.filter(pk=worker.pk).update(
            checked_at=timezone.now() - timedelta(minutes=1)
        )
    elif failure == "future":
        WorkerHealth.objects.filter(pk=worker.pk).update(
            checked_at=timezone.now() + timedelta(days=1)
        )
    elif failure == "wrong_policy":
        WorkerHealth.objects.filter(pk=worker.pk).update(fingerprint="c" * 64)
    else:
        WorkerHealth.objects.filter(pk=worker.pk).update(ready=False)
    # A different, fresh worker must never satisfy the named candidate check.
    WorkerHealth.objects.create(
        pk="old-worker",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    before = QueueLock.objects.values("pause_token", "paused_at").get(pk=1)
    output = io.StringIO()
    with pytest.raises(CommandError):
        call_command(
            "deployment_check", token=token, worker_id="candidate-check", timeout=0, stdout=output
        )
    assert QueueLock.objects.values("pause_token", "paused_at").get(pk=1) == before
    assert not get_user_model().objects.exists()
    assert not Submission.objects.exists()
    assert not Problem.objects.exists()
    assert not ProblemVersion.objects.exists()
    assert output.getvalue() == ""


@pytest.mark.parametrize("failure", ["disabled", "backend", "unpinned"])
def test_configuration_rejected_before_state_changes(candidate, settings, failure):
    token, worker = candidate
    if failure == "disabled":
        settings.JUDGE_ENABLED = False
    elif failure == "backend":
        settings.JUDGE_BACKEND = "gvisor"
    else:
        settings.JUDGE_WASI_SHA256 = "untrusted"
    with pytest.raises(CommandError):
        call_command("deployment_check", token=token, worker_id=worker.pk)
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert not get_user_model().objects.exists()
    assert not Submission.objects.exists()


@pytest.mark.parametrize("timeout", [-1, 601, float("nan"), float("inf"), "invalid"])
def test_invalid_timeout_does_not_change_admission(candidate, timeout):
    token, worker = candidate
    with pytest.raises(CommandError):
        call_command("deployment_check", token=token, worker_id=worker.pk, timeout=timeout)
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert not get_user_model().objects.exists()


def test_pending_real_user_work_is_never_cancelled_by_candidate_check(candidate, catalog, user):
    token, worker = candidate
    job = Submission.objects.create(
        user=user,
        version=ProblemVersion.objects.first(),
        source="private source",
        source_digest="a" * 64,
        evaluation_fingerprint=configured_fingerprint(),
        mode="submit",
    )
    with pytest.raises(CommandError, match="completely drained"):
        call_command("deployment_check", token=token, worker_id=worker.pk, timeout=0)
    job.refresh_from_db()
    assert job.status == "queued" and job.source == "private source"
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert list(get_user_model().objects.values_list("pk", flat=True)) == [user.pk]
    assert not Problem.objects.filter(pk__startswith="deploy-check-").exists()


def test_missing_executor_times_out_and_cleans_only_synthetic_records(candidate, catalog, user):
    token, worker = candidate
    pause_before = QueueLock.objects.values("pause_token", "paused_at").get(pk=1)
    problems_before = list(Problem.objects.values("id", "current_id", "published"))
    versions_before = list(ProblemVersion.objects.values("id", "digest", "data"))
    # This worker record deliberately has no process. It must never manufacture
    # results: timeout is the expected outcome and emits no successful receipt.
    output = io.StringIO()
    with pytest.raises(CommandError, match="evaluation timed out"):
        call_command(
            "deployment_check",
            token=token.hex.upper(),
            worker_id=worker.pk,
            timeout=0,
            stdout=output,
        )
    assert QueueLock.objects.values("pause_token", "paused_at").get(pk=1) == pause_before
    assert list(get_user_model().objects.values_list("pk", flat=True)) == [user.pk]
    assert not Submission.objects.exists()
    assert list(Problem.objects.values("id", "current_id", "published")) == problems_before
    assert list(ProblemVersion.objects.values("id", "digest", "data")) == versions_before
    assert output.getvalue() == ""


def test_smoke_content_uses_fixed_non_importable_evaluator_and_bounded_cases():
    problem = smoke_problem("deploy-check-" + uuid.uuid4().hex)
    assert problem["evaluator"] == "python-function-v1"
    assert problem["function"] == "solve"
    assert problem["tests"] == [
        {"name": "public", "args": [0], "expected": 1, "public": True},
        {"name": "protected", "args": [-7], "expected": -6, "public": False},
    ]


@pytest.fixture
def interrupted_fixture(candidate):
    token, worker = candidate
    identifier = fixture_identifier(token, worker.pk)
    data = smoke_problem(identifier)
    actor = get_user_model().objects.create_user(username=identifier, password=None)
    problem = Problem.objects.create(pk=identifier)
    version = ProblemVersion.objects.create(
        problem=problem, revision=1, digest=digest(data), data=data
    )
    problem.current = version
    problem.save()
    for source in (CORRECT, WRONG):
        Submission.objects.create(
            user=actor,
            version=version,
            source=source,
            source_digest=hashlib.sha256(source.encode()).hexdigest(),
            evaluation_fingerprint=worker.fingerprint,
            mode="submit",
        )
    return actor, problem, version


def test_operation_fixture_identity_is_stable_and_scoped():
    token = uuid.uuid4()
    assert fixture_identifier(token, "worker-a") == fixture_identifier(token, "worker-a")
    assert fixture_identifier(token, "worker-a") != fixture_identifier(token, "worker-b")
    assert fixture_identifier(token, "worker-a") != fixture_identifier(uuid.uuid4(), "worker-a")


def test_retry_reconciles_exact_interrupted_fixture_before_new_check(
    candidate, interrupted_fixture, user
):
    token, worker = candidate
    before = QueueLock.objects.values("pause_token", "paused_at").get(pk=1)
    with pytest.raises(CommandError, match="evaluation timed out"):
        call_command("deployment_check", token=token, worker_id=worker.pk, timeout=0)
    assert QueueLock.objects.values("pause_token", "paused_at").get(pk=1) == before
    assert list(get_user_model().objects.values_list("pk", flat=True)) == [user.pk]
    assert not Submission.objects.exists()
    assert not ProblemVersion.objects.exists()
    assert not Problem.objects.exists()


@pytest.mark.parametrize(
    "collision", ["usable_password", "version", "source", "mode", "extra_job", "private_draft"]
)
def test_retry_refuses_unrecognized_collision_without_deleting_anything(
    candidate, interrupted_fixture, other_user, collision
):
    token, worker = candidate
    actor, problem, version = interrupted_fixture
    if collision == "usable_password":
        actor.set_password("A-real-usable-password-16275")
        actor.save(update_fields=["password"])
    elif collision == "version":
        version.data["statement"] = "Unrelated content"
        version.save(update_fields=["data"])
    elif collision == "source":
        Submission.objects.filter(source=WRONG).update(source="unrelated source")
    elif collision == "mode":
        Submission.objects.filter(source=WRONG).update(mode="run")
    elif collision == "extra_job":
        Submission.objects.create(
            user=actor,
            version=version,
            source="another private submission",
            source_digest="a" * 64,
            mode="submit",
        )
    else:
        Draft.objects.create(user=other_user, version=version, source="Unrelated private draft")
    users_before = list(get_user_model().objects.values())
    jobs_before = list(Submission.objects.values())
    problem_before = Problem.objects.values().get(pk=problem.pk)
    version_before = ProblemVersion.objects.values().get(pk=version.pk)
    drafts_before = list(Draft.objects.values())
    with pytest.raises(CommandError, match="Existing deployment"):
        call_command("deployment_check", token=token, worker_id=worker.pk, timeout=0)
    assert list(get_user_model().objects.values()) == users_before
    assert list(Submission.objects.values()) == jobs_before
    assert Problem.objects.values().get(pk=problem.pk) == problem_before
    assert ProblemVersion.objects.values().get(pk=version.pk) == version_before
    assert list(Draft.objects.values()) == drafts_before
    assert QueueLock.objects.get(pk=1).pause_token == token


@pytest.mark.django_db(transaction=True)
def test_retry_cleans_revoked_retained_fixture_after_worker_acknowledgement(
    candidate, interrupted_fixture
):
    if connection.vendor != "postgresql":
        pytest.skip("Restart cleanup acknowledgements require PostgreSQL transactions")
    token, worker = candidate
    actor, problem, _ = interrupted_fixture
    actor.delete()
    problem.current = None
    problem.published = False
    problem.save(update_fields=["current", "published"])
    stopped = Event()

    def heartbeat_only():
        # No executor or verdicts: cleanup may proceed, but the new real check
        # must still time out rather than accept the administrative heartbeat.
        try:
            while not stopped.wait(0.05):
                WorkerHealth.objects.filter(pk=worker.pk).update(checked_at=timezone.now())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        heartbeat = executor.submit(heartbeat_only)
        try:
            with pytest.raises(CommandError, match="evaluation timed out"):
                call_command("deployment_check", token=token, worker_id=worker.pk, timeout=0)
        finally:
            stopped.set()
            heartbeat.result(timeout=5)
    assert not get_user_model().objects.exists()
    assert not Submission.objects.exists()
    assert not ProblemVersion.objects.exists()
    assert not Problem.objects.exists()
    assert QueueLock.objects.get(pk=1).pause_token == token


@pytest.mark.django_db(transaction=True)
def test_running_check_revokes_own_lease_before_removing_version(candidate):
    if connection.vendor != "postgresql":
        pytest.skip("Lease revocation and cleanup races require PostgreSQL transactions")
    token, worker = candidate
    fingerprint = worker.fingerprint

    def lease_participant():
        # This participant exercises real DB leases, with no interpreter and no
        # fabricated verdict. The command must time out, never report success.
        try:
            deadline = time.monotonic() + 5
            job = None
            while job is None and time.monotonic() < deadline:
                job = claim(fingerprint)
                if job is None:
                    time.sleep(0.01)
            assert job is not None
            assert QueueLock.objects.get(pk=1).pause_token == token
            assert not get_user_model().objects.get(pk=job.user_id).has_usable_password()
            while Submission.objects.filter(pk=job.pk).exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            # The account deletion has committed, but no acknowledgement was
            # emitted yet. Removing the version now would race lazy worker reads.
            assert ProblemVersion.objects.filter(pk=job.version_id).exists()
            assert Problem.objects.get(pk=job.version.problem_id).published is False
        finally:
            WorkerHealth.objects.filter(pk=worker.pk).update(checked_at=timezone.now())
            connections.close_all()

    output = io.StringIO()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(lease_participant)
        with pytest.raises(CommandError, match="evaluation timed out"):
            call_command(
                "deployment_check",
                token=token,
                worker_id=worker.pk,
                timeout=1,
                stdout=output,
            )
        pending.result(timeout=5)
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert not get_user_model().objects.exists()
    assert not Submission.objects.exists()
    assert not ProblemVersion.objects.exists()
    assert not Problem.objects.exists()
    assert output.getvalue() == ""
