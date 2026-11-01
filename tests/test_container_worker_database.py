"""Actual PostgreSQL role and schema checks; no device access or receipt creation."""

import uuid
from contextlib import contextmanager

import pytest
from django.core.management import call_command
from django.db import ProgrammingError, connection
from django.utils import timezone

from core.container_worker_database import GRANTS, check_database
from core.management.commands import container_gpu_worker as command
from core.models import GPUDevice, Submission
from tests.gpu_evidence_fixtures import version_fixture
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_readiness import startup as startup
from tests.test_container_worker import worker as worker
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def role():
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL role privileges")
    identity = "px_worker_" + uuid.uuid4().hex[:16]
    quoted = connection.ops.quote_name(identity)
    database = connection.ops.quote_name(connection.settings_dict["NAME"])
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {quoted} NOLOGIN")
        cursor.execute(f"REVOKE TEMP ON DATABASE {database} FROM PUBLIC")
        cursor.execute(f"GRANT CONNECT ON DATABASE {database} TO {quoted}")
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {quoted}")
        for table, grants in GRANTS.items():
            cursor.execute(f"GRANT {', '.join(sorted(grants))} ON {table} TO {quoted}")
    try:
        yield quoted
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute(f"DROP OWNED BY {quoted}")
            cursor.execute(f"DROP ROLE {quoted}")
            cursor.execute(f"GRANT TEMP ON DATABASE {database} TO PUBLIC")


@contextmanager
def active_role(role):
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {role}")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_existing_worker_grants_pass_readonly_preflight(role):
    before = list(GPUDevice.objects.values())
    with active_role(role):
        check_database()
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone() == ("off",)
    assert list(GPUDevice.objects.values()) == before


@pytest.mark.parametrize(
    "defect",
    [
        "table-read",
        "column-read",
        "grant-option",
        "missing-write",
        "create",
        "temporary",
        "sequence",
        "superuser",
        "missing-column",
    ],
)
def test_wrong_permissions_or_schema_fail_closed(role, defect):
    database = connection.ops.quote_name(connection.settings_dict["NAME"])
    command = {
        "table-read": f"GRANT SELECT ON auth_user TO {role}",
        "column-read": f"GRANT SELECT (password) ON auth_user TO {role}",
        "grant-option": f"GRANT SELECT ON core_submission TO {role} WITH GRANT OPTION",
        "missing-write": f"REVOKE UPDATE ON core_submission FROM {role}",
        "create": f"GRANT CREATE ON SCHEMA public TO {role}",
        "temporary": f"GRANT TEMP ON DATABASE {database} TO {role}",
        "sequence": f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
        "superuser": f"ALTER ROLE {role} SUPERUSER",
        "missing-column": "ALTER TABLE core_gpudevice RENAME COLUMN environment TO broken_environment",
    }[defect]
    with connection.cursor() as cursor:
        cursor.execute(command)
    try:
        with active_role(role), pytest.raises((ValueError, ProgrammingError)):
            check_database()
    finally:
        if defect == "missing-column":
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE core_gpudevice RENAME COLUMN broken_environment TO environment"
                )


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_worker_role_completes_claim_settlement_or_cancellation(
    role,
    worker,
    user,
    monkeypatch,
    cancel,
):
    job = queued(user, version_fixture(), evaluation_fingerprint=worker.adapter.fingerprint)
    monkeypatch.setattr(command, "check_database", check_database)
    if cancel:

        def expire(task, _request):
            worker.slot = job.id, task.service.reservation_token
            Submission.objects.filter(pk=job.id).update(lease_until=timezone.now())

        worker.task_hook = expire
    with active_role(role):
        call_command("container_gpu_worker", once=True)
    job.refresh_from_db()
    assert job.budget_state == "consumed"
    assert (job.verdict == "accepted") == (not cancel)
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.job_id is None
    assert not worker.path.exists() and worker.closed == 1
