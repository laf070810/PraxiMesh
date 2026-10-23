"""Actual PostgreSQL role grants and concurrent participation, in a disposable database."""

import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client

from challenges.catalog import import_package, load_package
from challenges.models import Challenge, Participation
from scripts.deploy_database import WORKER_PRIVILEGES, web_read_tables, web_write_tables

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Actual PostgreSQL required"),
]


@pytest.fixture
def activity(catalog):
    from datetime import timedelta

    from django.utils import timezone

    data = load_package(settings.CONTENT_ROOT / "challenges.json")
    data["challenges"][0]["starts_at"] = (timezone.now() - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data["challenges"][0]["ends_at"] = (timezone.now() + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    import_package(data)
    return Challenge.objects.select_related("current").get(pk="practice-2026")


def test_runtime_web_can_join_but_cannot_publish_and_worker_cannot_read(activity, user):
    from django.core.management import call_command

    suffix = uuid.uuid4().hex[:12]
    web, worker = "challenge_web_" + suffix, "challenge_worker_" + suffix
    tables = ("challenges_challenge", "challenges_challengerevision", "challenges_challengetask")
    with connection.cursor() as cursor:
        for role in (web, worker):
            cursor.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
            cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        for table in web_read_tables(connection.connection):
            cursor.execute(f'GRANT SELECT ON "{table}" TO "{web}"')
        for table in web_write_tables(connection.connection):
            cursor.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{web}"')
        cursor.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{web}"')
        for table, permissions in WORKER_PRIVILEGES.items():
            cursor.execute(f'GRANT {permissions} ON "{table}" TO "{worker}"')
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{web}"')
        try:
            for table in tables:
                with connection.cursor() as cursor:
                    cursor.execute(f'SELECT * FROM "{table}" LIMIT 1')
                for statement in (
                    f'UPDATE "{table}" SET id=id WHERE false',
                    f'DELETE FROM "{table}" WHERE false',
                ):
                    with (
                        pytest.raises(DatabaseError, match="permission denied"),
                        connection.cursor() as cursor,
                    ):
                        cursor.execute(statement)
            with pytest.raises(DatabaseError, match="permission denied"):
                call_command(
                    "challenge_import",
                    str(settings.CONTENT_ROOT / "challenges.json"),
                    stdout=io.StringIO(),
                )
            # Exercise real Django permission/session/CSRF-independent request code
            # using precisely the same table grants as the deployment controller.
            client = Client()
            client.force_login(user)
            endpoint = f"/challenges/{activity.pk}/revisions/1/participate/"
            response = client.post(endpoint, {"action": "join"})
            assert response.status_code == 302
            assert Participation.objects.filter(user=user).count() == 1
            assert client.get("/learning/achievements/").status_code == 200
            assert client.post(endpoint, {"action": "leave"}).status_code == 302
            assert not Participation.objects.exists()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{worker}"')
        try:
            for table in (*tables, "challenges_participation"):
                with (
                    pytest.raises(DatabaseError, match="permission denied"),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(f'SELECT * FROM "{table}" LIMIT 0')
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
    finally:
        with connection.cursor() as cursor:
            for role in (web, worker):
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE "{role}"')


def test_two_concurrent_joins_preserve_one_join_time(catalog, user):
    from datetime import timedelta

    from django.utils import timezone

    data = load_package(settings.CONTENT_ROOT / "challenges.json")
    plan = data["challenges"][0]
    plan["starts_at"] = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan["ends_at"] = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    import_package(data)
    barrier = Barrier(2)

    def join():
        close_old_connections()
        try:
            client = Client()
            client.force_login(user)
            barrier.wait(timeout=10)
            response = client.post(
                "/challenges/practice-2026/revisions/1/participate/", {"action": "join"}
            )
            return response.status_code, Participation.objects.get(user=user).joined_at
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: join(), range(2)))
    assert results[0] == results[1] and results[0][0] == 302
    assert Participation.objects.count() == 1
