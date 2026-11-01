"""Real PostgreSQL lock checks; no GPU execution or fabricated readiness receipt."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from django.core.management.base import CommandError
from django.db import connection, connections
from django.utils import timezone

from core import deployment_gpu_check as module
from core.management.commands.judge_admission import database_action
from core.models import GPUDevice, Problem, QueueLock
from core.queue import AdmissionError, enqueue
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_deployment_gpu_check import candidate as candidate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Actual PostgreSQL transaction and row/table locks required")


def isolated(action):
    connections.close_all()
    try:
        return action()
    finally:
        connections.close_all()


def test_candidate_enqueue_transaction_never_exposes_open_public_admission(
    candidate, catalog, user, monkeypatch
):
    version = Problem.objects.get(pk=catalog["problems"][0]["id"]).current
    holding, release, outside = Event(), Event(), Event()
    original = module.enqueue

    def diagnostic_enqueue(*args):
        result = original(*args)
        if not holding.is_set():
            assert QueueLock.objects.get(pk=1).pause_token is None
            holding.set()
            assert release.wait(5)
        return result

    def public_enqueue():
        outside.set()
        with pytest.raises(AdmissionError):
            enqueue(user, version, "def solve(value): return value", "submit")

    monkeypatch.setattr(module, "enqueue", diagnostic_enqueue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        preparing = pool.submit(isolated, lambda: database_action(candidate.probe().prepare, 5))
        try:
            assert holding.wait(5)
            waiting = pool.submit(isolated, public_enqueue)
            assert outside.wait(5)
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.1)
        finally:
            release.set()
        assert len(preparing.result(timeout=5)) == 2
        waiting.result(timeout=5)
    assert QueueLock.objects.get(pk=1).pause_token == candidate.token
    assert not user.submission_set.exists()


def test_observation_wait_holds_no_device_or_queue_lock(candidate, monkeypatch):
    def waiting(seconds):
        def heartbeat():
            return GPUDevice.objects.filter(pk=candidate.device.pk).update(
                checked_at=timezone.now()
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(isolated, heartbeat).result(timeout=2) == 1
        raise CommandError("Observed unlocked wait")

    monkeypatch.setattr(module.time, "sleep", waiting)
    with pytest.raises(CommandError, match="unlocked wait"):
        candidate.probe().run(timeout=1)
