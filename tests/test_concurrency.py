import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from core.models import ProblemVersion, QueueLock, Submission, WorkerHealth
from core.queue import AdmissionError, claim, configured_fingerprint, enqueue, pause_admission


@pytest.mark.django_db(transaction=True)
def test_parallel_admission_cannot_exceed_user_quota(catalog, user, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Production concurrency requires the PostgreSQL test matrix")
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    version = ProblemVersion.objects.first()

    def attempt(_):
        try:
            enqueue(user, version, "source", "submit")
            return True
        except AdmissionError:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(attempt, range(6)))
    assert sum(outcomes) == 2
    assert Submission.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_workers_cannot_claim_the_same_job(catalog, user):
    if connection.vendor != "postgresql":
        pytest.skip("Production concurrency requires the PostgreSQL test matrix")
    queued = Submission.objects.create(
        user=user,
        version=ProblemVersion.objects.first(),
        source="source",
        source_digest="a" * 64,
        evaluation_fingerprint=configured_fingerprint(),
        mode="submit",
    )

    def attempt(_):
        try:
            job = claim()
            return job.id if job else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))
    assert outcomes.count(queued.pk) == 1
    assert outcomes.count(None) == 1


@pytest.mark.django_db(transaction=True)
def test_pause_transaction_serializes_competing_admissions(catalog, user, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Production concurrency requires the PostgreSQL test matrix")
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    QueueLock.objects.create(id=1)
    version = ProblemVersion.objects.first()
    barrier = Barrier(7)

    def attempt():
        try:
            barrier.wait(timeout=10)
            enqueue(user, version, "source", "submit")
            return "admitted"
        except AdmissionError as error:
            return error.status
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=6) as executor:
        with transaction.atomic():
            pause_admission(uuid.uuid4())
            futures = [executor.submit(attempt) for _ in range(6)]
            barrier.wait(timeout=10)
        outcomes = [future.result(timeout=10) for future in futures]
    assert outcomes == [503] * 6
    assert Submission.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_committed_job_is_retained_when_pause_waits_for_admission(catalog, user, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Production concurrency requires the PostgreSQL test matrix")
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    barrier = Barrier(2)
    token = uuid.uuid4()

    def pause():
        try:
            barrier.wait(timeout=10)
            pause_admission(token)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            job = enqueue(user, ProblemVersion.objects.first(), "source", "submit")
            future = executor.submit(pause)
            barrier.wait(timeout=10)
        future.result(timeout=10)
    assert QueueLock.objects.get(pk=1).pause_token == token
    assert claim().pk == job.pk
    assert Submission.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_different_policy_workers_only_claim_their_own_jobs(catalog, user):
    if connection.vendor != "postgresql":
        pytest.skip("Production concurrency requires the PostgreSQL test matrix")
    fingerprints = ["a" * 64, "b" * 64]
    jobs = [
        Submission.objects.create(
            user=user,
            version=ProblemVersion.objects.first(),
            source="source",
            source_digest="a" * 64,
            evaluation_fingerprint=fingerprint,
            mode="submit",
        )
        for fingerprint in fingerprints
    ]
    barrier = Barrier(2)

    def attempt(fingerprint):
        try:
            barrier.wait(timeout=10)
            return claim(fingerprint).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, fingerprints))
    assert outcomes == [job.pk for job in jobs]
