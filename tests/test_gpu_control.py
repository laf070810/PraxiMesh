"""Lease/control tests only; no hardware recovery or GPU receipt is simulated."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from core import gpu_resources
from core.gpu_control import GPUJobControl, GPUJobLost, bounded_database
from core.models import GPUDevice, Submission
from judge.gpu_device_lock import DeviceLockError
from judge.gpu_session import GPUExecutionError
from scripts.deploy_database import worker_privileges
from tests.test_gpu_resources import DEVICE, queued
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import guard as guard

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def control(guard, gpu_version, user):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    return GPUJobControl(guard, job)


def timeout_settings():
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            )
        else:
            cursor.execute("PRAGMA busy_timeout")
        return cursor.fetchone()


@pytest.mark.parametrize("failure", [False, True])
def test_database_limits_apply_only_to_control_transaction_and_restore(failure):
    before = timeout_settings()
    try:
        with bounded_database():
            assert timeout_settings() == (
                ("250ms", "1s") if connection.vendor == "postgresql" else (250,)
            )
            if failure:
                raise RuntimeError("test rollback")
    except RuntimeError:
        assert failure
    assert timeout_settings() == before


def test_control_cannot_be_wrapped_in_a_long_outer_transaction(control):
    with transaction.atomic(), pytest.raises(ValueError, match="own short transaction"):
        control.poll()
    control.job.refresh_from_db()
    assert control.job.budget_state == "reserved"


def test_begin_is_idempotent_and_result_waits_for_explicit_recovery(control):
    control.poll()
    control.begin_execution()
    control.begin_execution()
    control.job.refresh_from_db()
    assert control.job.budget_state == "consumed"
    assert GPUDevice.objects.get(pk=DEVICE).job_id == control.job.pk
    # This checks the trusted caller's DB transition, not physical recovery.
    assert control.finish_after_recovery({"verdict": "internal_error"}, {}) == 1
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "ready" and device.job_id is None


def test_unstarted_nonplatform_result_cannot_free_the_reserved_device(control):
    with pytest.raises(ValueError, match="recorded execution start"):
        control.finish_after_recovery({"verdict": "accepted"}, {})
    control.job.refresh_from_db()
    assert control.job.status == "running" and control.job.budget_state == "reserved"
    assert GPUDevice.objects.get(pk=DEVICE).job_id == control.job.pk


@pytest.mark.parametrize("deleted", [False, True])
def test_cancelled_or_deleted_job_retains_slot_until_recovery_call(control, deleted):
    control.begin_execution()
    if deleted:
        control.job.user.delete()
    else:
        Submission.objects.filter(pk=control.job.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    with pytest.raises(GPUJobLost):
        control.poll()
    device = GPUDevice.objects.get(pk=DEVICE)
    assert device.state == "quarantined" and device.job_id == control.job.pk
    control.release_after_recovery()  # DB contract only; does not certify hardware.
    device.refresh_from_db()
    assert device.state == "ready" and device.job_id is None
    if not deleted:
        control.job.refresh_from_db()
        assert control.job.verdict == "internal_error" and control.job.budget_state == "consumed"


def test_slot_time_crossed_inside_renewal_rolls_back_changes(control, monkeypatch):
    old_lease = control.job.lease_until
    original = gpu_resources.begin_execution

    def crossed(*args):
        value = original(*args)
        control.deadline = time.monotonic() - 1
        return value

    monkeypatch.setattr(gpu_resources, "begin_execution", crossed)
    with pytest.raises(GPUExecutionError):
        control.begin_execution()
    control.job.refresh_from_db()
    assert control.job.lease_until == old_lease
    assert control.job.budget_state == "reserved" and control.job.execution_started_at is None


def test_preparation_time_is_not_reset_and_cleanup_keeps_reserved_margin(control):
    control.job.started_at = timezone.now() - timedelta(seconds=181)
    with pytest.raises(GPUExecutionError):
        GPUJobControl(control.guard, control.job)
    control.deadline = time.monotonic() + 5
    with pytest.raises(GPUExecutionError):
        control.poll()
    assert control.finish_after_recovery({"verdict": "internal_error"}, {}) == 1


def test_lost_host_lock_cannot_change_database_state(control):
    control.guard.close()
    with pytest.raises(DeviceLockError):
        control.quarantine()
    assert GPUDevice.objects.get(pk=DEVICE).state == "reserved"


@pytest.mark.parametrize("locked", ["device", "job"])
def test_real_postgres_row_contention_times_out_without_releasing_device(control, locked):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row contention")

    def attempt():
        try:
            control.poll()
        finally:
            connections.close_all()

    model, key = (GPUDevice, DEVICE) if locked == "device" else (Submission, control.job.pk)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            model.objects.select_for_update().get(pk=key)
            started = time.monotonic()
            future = executor.submit(attempt)
            with pytest.raises(DatabaseError) as error:
                future.result(timeout=3)
            assert error.value.__cause__.sqlstate == "55P03"
            assert 0.18 < time.monotonic() - started < 3
    control.job.refresh_from_db()
    assert control.job.budget_state == "reserved"
    assert GPUDevice.objects.get(pk=DEVICE).job_id == control.job.pk


def test_real_postgres_statement_timeout_restores_settings():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL statement timeout")
    before = timeout_settings()
    started = time.monotonic()
    with pytest.raises(DatabaseError) as error, bounded_database(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_sleep(3)")
    assert error.value.__cause__.sqlstate == "57014"
    assert 0.8 < time.monotonic() - started < 3
    assert timeout_settings() == before


def test_postgres_worker_role_can_use_bounded_callbacks_without_broader_grants(control):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL role permissions")
    role = connection.ops.quote_name("px_gpu_control_" + uuid.uuid4().hex[:16])
    try:
        grants = worker_privileges(connection.connection)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE ROLE {role} NOLOGIN")
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            for table, permissions in grants.items():
                cursor.execute(
                    f"GRANT {permissions} ON {connection.ops.quote_name(table)} TO {role}"
                )
            cursor.execute(f"SET ROLE {role}")
        control.poll()
        control.begin_execution()
        assert control.finish_after_recovery({"verdict": "internal_error"}, {}) == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute(f"DROP OWNED BY {role}")
            cursor.execute(f"DROP ROLE {role}")
