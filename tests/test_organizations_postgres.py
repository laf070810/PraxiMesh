"""PostgreSQL-only lock/owner-role checks against an isolated test database.

Run with DATABASE_URL for a disposable owner connection. Roles created below
exist only for each test and are dropped; no role credentials are persisted.
"""

import io
import queue
import threading
import time

import pytest
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction

from core.content import import_package
from core.models import Problem
from organizations import publishing
from organizations.models import ManagedObject, PackageRevision, PublicationLock
from tests.test_organizations import approved, publish

pytest_plugins = ["tests.test_organizations"]
pytestmark = [pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def postgres_only():
    if connection.vendor != "postgresql":
        pytest.skip("Requires a disposable PostgreSQL owner database")
    PublicationLock.objects.get_or_create(pk=1)


def test_real_publish_and_official_import_wait_for_shared_lock(
    org, user, reviewer, payload, catalog
):
    revision = approved(user, org, reviewer, payload)
    results = queue.Queue()
    names = ["organization-package-lock-test", "organization-official-lock-test"]

    def run(name, operation):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET application_name = %s", [name])
                cursor.execute("SET lock_timeout = '10s'")
            operation()
            results.put((name, None))
        except Exception as error:
            results.put((name, error))
        finally:
            connection.close()

    threads = []
    try:
        with transaction.atomic():
            publishing.publication_lock()
            threads = [
                threading.Thread(target=run, args=(names[0], lambda: publish(revision))),
                threading.Thread(target=run, args=(names[1], lambda: import_package(catalog))),
            ]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_stat_clear_snapshot()")
                    cursor.execute(
                        "SELECT application_name FROM pg_stat_activity "
                        "WHERE application_name = ANY(%s) AND wait_event_type = 'Lock'",
                        [names],
                    )
                    waiting = {row[0] for row in cursor.fetchall()}
                if waiting == set(names):
                    break
                assert time.monotonic() < deadline, (
                    "Both publishers must demonstrably wait on PostgreSQL locks"
                )
                time.sleep(0.02)
            assert results.empty()
            assert not ManagedObject.objects.exists()
    finally:
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
    outcomes = [results.get_nowait() for _ in threads]
    assert all(error is None for _, error in outcomes), outcomes
    revision.refresh_from_db()
    assert revision.state == PackageRevision.State.PUBLISHED
    assert Problem.objects.get(pk="sample-org--basics--positive-sum").published
    assert Problem.objects.get(pk="sum-positive").published
    assert ManagedObject.objects.count() == 3


def test_web_owner_command_is_denied_by_database_and_worker_cannot_read(
    org, user, reviewer, payload
):
    revision = approved(user, org, reviewer, payload)
    roles = ["organizations_acceptance_web", "organizations_acceptance_worker"]
    with connection.cursor() as cursor:
        for role in roles:
            cursor.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
            cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        cursor.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO organizations_acceptance_web"
        )
        # Proposed Web grant: organization workflow tables are writable, while
        # managed identities, publication locks and core content remain read-only.
        for table in connection.introspection.table_names():
            if table.startswith("organizations_") and table not in {
                "organizations_managedobject",
                "organizations_publicationlock",
            }:
                cursor.execute(
                    f'GRANT INSERT, UPDATE, DELETE ON "{table}" TO organizations_acceptance_web'
                )
        cursor.execute(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO organizations_acceptance_web"
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE organizations_acceptance_web")
        try:
            with pytest.raises(DatabaseError, match="permission denied"):
                call_command(
                    "org_package_publish",
                    str(revision.pk),
                    digest=revision.digest,
                    stdout=io.StringIO(),
                )
            for table in [
                "core_problem",
                "core_problemversion",
                "core_knowledgenode",
                "organizations_managedobject",
                "organizations_publicationlock",
            ]:
                with (
                    pytest.raises(DatabaseError, match="permission denied"),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(f'UPDATE "{table}" SET id=id WHERE false')
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
        assert not ManagedObject.objects.exists()
        revision.refresh_from_db()
        assert revision.state == PackageRevision.State.APPROVED
        publish(revision)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE organizations_acceptance_worker")
        try:
            with (
                pytest.raises(DatabaseError, match="permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT * FROM organizations_courseenrollment")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
    finally:
        with connection.cursor() as cursor:
            for role in roles:
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE "{role}"')
