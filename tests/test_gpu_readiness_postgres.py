"""Actual PostgreSQL publication/ready race; runtime remains a test double."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import DatabaseError, connection, connections, transaction

from core import gpu_resources
from core.gpu_readiness import heartbeat_ready
from core.models import GPUDevice
from judge.gpu_receipt import ReceiptStore
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_readiness import startup as container_startup  # noqa: F401
from tests.test_gpu_readiness import startup as host_startup  # noqa: F401
from tests.test_gpu_resources import gpu_version as gpu_version

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["host_startup", "container_startup"], ids=["host", "container"])
def startup(request):
    return request.getfixturevalue(request.param)


def test_row_lock_after_file_publication_cannot_publish_ready(startup, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL row locks")
    acquired, release = Event(), Event()
    timing = []
    original = ReceiptStore._write

    def lock_device():
        try:
            with transaction.atomic():
                GPUDevice.objects.select_for_update().get(pk=startup.guard.device_uuid)
                acquired.set()
                assert release.wait(5), "Readiness test did not release its owned row lock"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = []

        def publish_then_lock(store, encoded):
            original(store, encoded)
            futures.append(executor.submit(lock_device))
            assert acquired.wait(3)
            timing.append(time.monotonic())

        monkeypatch.setattr(ReceiptStore, "_write", publish_then_lock)
        try:
            with pytest.raises(DatabaseError):
                startup.run()
            assert timing and time.monotonic() - timing[0] < 2.5
            assert not startup.path.exists()
        finally:
            release.set()
            for future in futures:
                future.result(timeout=3)
    device = GPUDevice.objects.get(pk=startup.guard.device_uuid)
    assert device.state == "quarantined" and device.checked_at is None
    assert (device.job_id, device.reservation_token) == (startup.job.id, startup.job.lease_token)
    startup.job.refresh_from_db()
    assert startup.job.status == "running" and startup.job.budget_state == "reserved"


def test_idle_heartbeat_holds_identity_row_until_health_refresh(startup, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL row locks")
    startup.run()
    attempting, changed = Event(), Event()
    blocked = []
    original = gpu_resources.heartbeat

    def change_worker():
        try:
            attempting.set()
            GPUDevice.objects.filter(pk=startup.guard.device_uuid).update(worker_id="replacement")
            changed.set()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = []

        def refresh_after_identity_check(guard):
            futures.append(executor.submit(change_worker))
            assert attempting.wait(2)
            blocked.append(not changed.wait(0.3))
            return original(guard)

        monkeypatch.setattr(gpu_resources, "heartbeat", refresh_after_identity_check)
        try:
            assert heartbeat_ready(
                startup.guard, startup.worker, startup.adapter, receipt_directory=startup.directory
            )
        finally:
            for future in futures:
                future.result(timeout=3)
    assert blocked == [True], "Identity changed between its check and the health update"
    assert changed.is_set()
