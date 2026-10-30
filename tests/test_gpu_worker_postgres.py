"""Actual PostgreSQL worker identity fencing; runtime remains a test double."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection, connections

from core import gpu_resources
from core.gpu_readiness import claim_ready, initialize_controller
from core.models import GPUDevice
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_worker import worker as worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("phase", ["claim", "finish"])
def test_identity_row_stays_locked_until_claim_or_settlement_commit(worker, monkeypatch, phase):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL row locks")
    target = worker.guard
    worker.enqueue()
    if phase == "claim":
        initialize_controller(
            worker.guard, worker.worker_id, worker.adapter, receipt_directory=worker.directory
        )
    attempting, changed = Event(), Event()
    blocked = []
    original = getattr(gpu_resources, phase)

    def replace_identity():
        try:
            attempting.set()
            GPUDevice.objects.filter(pk=target.device_uuid).update(worker_id="replacement")
            changed.set()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = []

        def mutate_after_identity_check(*args, **kwargs):
            # Called with the checked device row already held by the worker.
            futures.append(executor.submit(replace_identity))
            assert attempting.wait(2)
            blocked.append(not changed.wait(0.3))
            return original(*args, **kwargs)

        monkeypatch.setattr(gpu_resources, phase, mutate_after_identity_check)
        try:
            if phase == "claim":
                assert (
                    claim_ready(
                        worker.guard,
                        worker.worker_id,
                        worker.adapter,
                        receipt_directory=worker.directory,
                    )
                    is not None
                )
            else:
                worker.run(once=True)
        finally:
            for future in futures:
                future.result(timeout=3)
    assert blocked == [True], "Worker identity changed before the checked transition committed"
    assert changed.is_set()
