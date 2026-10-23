"""Exercise the production table grants on the new reservation state machine."""

import uuid

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from core.models import Problem, Submission, WorkerHealth
from core.queue import begin_execution, claim, configured_fingerprint, enqueue, finish
from scripts.deploy_database import WEB_READ, WEB_WRITE, WORKER_PRIVILEGES


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("slug", ["train-linear-model", "approved-ledger-report"])
def test_web_can_reserve_worker_can_consume_and_account_deletion_cascades(
    catalog, user, settings, slug
):
    if connection.vendor != "postgresql":
        pytest.skip("Production PostgreSQL minimum-role matrix")
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    WorkerHealth.objects.create(
        id="role-test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    version = Problem.objects.get(pk=slug).current
    names = ["px_c1_test_" + uuid.uuid4().hex[:16] for _ in range(2)]
    web, worker = map(connection.ops.quote_name, names)
    try:
        with connection.cursor() as cursor:
            for name in (web, worker):
                cursor.execute(f"CREATE ROLE {name} NOLOGIN")
                cursor.execute(f"GRANT USAGE ON SCHEMA public TO {name}")
            for name in WEB_READ:
                cursor.execute(f"GRANT SELECT ON {connection.ops.quote_name(name)} TO {web}")
            for name in WEB_WRITE:
                cursor.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {connection.ops.quote_name(name)} TO {web}"
                )
            cursor.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {web}")
            for name, privileges in WORKER_PRIVILEGES.items():
                cursor.execute(
                    f"GRANT {privileges} ON {connection.ops.quote_name(name)} TO {worker}"
                )
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {web}")
            job = enqueue(user, version, "private", "submit")
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {worker}")
            running = claim()
            assert running.pk == job.pk and begin_execution(running)
            assert finish(running, {"verdict": "internal_error"}, {}) == 1
        job.refresh_from_db()
        assert job.budget_state == "consumed"
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {worker}")
            user.delete()
        assert Submission.objects.filter(pk=job.pk).exists()
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {web}")
            user.delete()
        assert not Submission.objects.filter(pk=job.pk).exists()
    finally:
        with connection.cursor() as cursor:
            for name in (web, worker):
                cursor.execute(f"DROP OWNED BY {name}")
                cursor.execute(f"DROP ROLE {name}")
