"""Owned PostgreSQL migration and role checks; synthetic records, no GPU work."""

import uuid

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from test_showcase import enable, url
from test_showcase import job as cpu_job

from scripts.deploy_database import WORKER_PRIVILEGES, web_read_tables, web_write_tables
from showcase import services
from showcase.models import SharedResult
from tests.gpu_evidence_fixtures import job_fixture, version_fixture

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Actual PostgreSQL required"),
]


def test_upgrade_preserves_cpu_scope_and_old_writer_default(catalog, logged_client, user):
    enable(logged_client, user)
    cpu, next_cpu = cpu_job(user), cpu_job(user)
    gpu = job_fixture(user, version_fixture())
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [("showcase", "0001_initial")]
    executor.migrate(old_target)
    try:
        old = executor.loader.project_state(old_target).apps.get_model("showcase", "SharedResult")
        prior_cpu = old.objects.create(submission_id=cpu.pk, is_public=True)
        prior_gpu = old.objects.create(submission_id=gpu.pk, is_public=True)
        MigrationExecutor(connection).migrate(latest)
        for pk in (prior_cpu.pk, prior_gpu.pk):
            assert SharedResult.objects.get(pk=pk).metric_kind == SharedResult.Metric.SEQUENTIAL
        assert Client().get(url("entry", prior_cpu.pk)).status_code == 200
        assert Client().get(url("entry", prior_gpu.pk)).status_code == 404
        # Execute an actual old ORM INSERT which has no knowledge of the column.
        compatibility = old.objects.create(submission_id=next_cpu.pk, is_public=False)
        assert (
            SharedResult.objects.get(pk=compatibility.pk).metric_kind
            == SharedResult.Metric.SEQUENTIAL
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SharedResult.objects.filter(pk=compatibility.pk).update(metric_kind="unknown")
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_existing_web_grants_allow_confirm_withdraw_and_worker_cannot_read(user):
    profile = services.save_profile(
        user,
        {
            "revision": 0,
            "public_name": "GPU test profile",
            "bio": "Synthetic evidence only",
            "is_public": True,
            "confirm_public": True,
        },
    )
    item = job_fixture(user, version_fixture())
    suffix = uuid.uuid4().hex[:12]
    web, worker = "gpu_share_web_" + suffix, "gpu_share_worker_" + suffix
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
            entry = services.share(
                user, item.pk, profile_revision=profile.revision, entry_revision=0, confirmed=True
            )
            assert entry.metric_kind == SharedResult.Metric.GPU_BATCH
            assert services.public_card(entry)["score_ms"] == "3.000"
            services.withdraw(user, entry.pk, entry.revision)
            assert not services.public_entries().exists()
            with (
                pytest.raises(DatabaseError, match="permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute("UPDATE core_gpudevice SET worker_id=worker_id WHERE false")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{worker}"')
        try:
            for sql in (
                "SELECT * FROM showcase_sharedresult LIMIT 0",
                "SELECT * FROM showcase_publicprofile LIMIT 0",
                "UPDATE showcase_sharedresult SET metric_kind='gpu-batch-ms-v1' WHERE false",
            ):
                with (
                    pytest.raises(DatabaseError, match="permission denied"),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(sql)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
    finally:
        with connection.cursor() as cursor:
            for role in (web, worker):
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE "{role}"')
