"""Actual table/row lock interleavings; no GPU or readiness receipt."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError
from django.db import connection, connections, transaction

from core import gpu_resources
from core.gpu_control import bounded_database
from core.management.commands.judge_admission import database_action
from core.models import GPUDevice
from tests.test_gpu_resources import DEVICE
from tests.test_gpu_resources import guard as guard

pytestmark = pytest.mark.django_db(transaction=True)


def test_candidate_table_gate_cannot_interpose_between_device_row_lock_and_heartbeat(guard):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL table and row locks")
    entered, acquired = Event(), Event()
    identity = SimpleNamespace(pid=None)

    def candidate_gate():
        connections.close_all()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                identity.pid = cursor.fetchone()[0]
                entered.set()
                cursor.execute("LOCK TABLE core_gpudevice IN SHARE ROW EXCLUSIVE MODE")
                acquired.set()
                assert (
                    GPUDevice.objects.select_for_update().get(pk=DEVICE).owner_token
                    == guard.owner_token
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with bounded_database():
            GPUDevice.objects.select_for_update().get(pk=DEVICE)
            future = pool.submit(candidate_gate)
            assert entered.wait(5)
            deadline = time.monotonic() + 2
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT granted FROM pg_locks WHERE pid=%s AND relation='core_gpudevice'::regclass "
                        "AND mode='ShareRowExclusiveLock'",
                        [identity.pid],
                    )
                    waiting = cursor.fetchall()
                if waiting:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            # Before the fix the table lock is granted, and this exact heartbeat
            # UPDATE waits on it until the original 250 ms budget expires.
            assert gpu_resources.heartbeat(guard) is True
            assert waiting == [(False,)] and not acquired.is_set()
        future.result(timeout=5)
    assert acquired.is_set()


def test_reviewed_legacy_cleanup_releases_its_gate_before_original_heartbeat_timeout(guard):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL table and row locks")
    acquired = Event()

    def legacy_cleanup():
        connections.close_all()
        try:

            def gate():
                with connection.cursor() as cursor:
                    cursor.execute("LOCK TABLE core_gpudevice IN SHARE ROW EXCLUSIVE MODE")
                acquired.set()
                GPUDevice.objects.select_for_update().get(pk=DEVICE)

            with pytest.raises(CommandError):
                database_action(gate, 0.2)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        # The exact historical order, deliberately without the new table lock.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '250ms'")
            cursor.execute("SET LOCAL statement_timeout = '1s'")
            GPUDevice.objects.select_for_update().get(pk=DEVICE)
            future = pool.submit(legacy_cleanup)
            assert acquired.wait(5)
            assert gpu_resources.heartbeat(guard) is True
        future.result(timeout=5)
    assert GPUDevice.objects.get(pk=DEVICE).job_id is None
