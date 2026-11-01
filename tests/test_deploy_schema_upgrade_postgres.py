"""Real F-schema copy/upgrade on the owned PG runner; no old image or GPU."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import psycopg
import pytest
from django.db.migrations.loader import MigrationLoader
from psycopg import sql

from tests.deploy_schema_fixture import baseline_targets
from tests.test_deploy_database_postgres import owned_pg as owned_pg

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[1]
ADDED = {
    ("core", "0004_gpu_resources"),
    ("core", "0005_gpu_admission_environment"),
    ("showcase", "0002_shared_metric_scope"),
}
FRAMEWORK_TABLES = {"django_migrations", "django_content_type", "auth_permission"}


def test_f_migration_inventory_matches_unchanged_sources_and_graph():
    loader = MigrationLoader(None)
    _, historical = baseline_targets(loader)
    assert set(loader.graph.nodes) - historical == ADDED


def command(item, name, *, baseline=False):
    database = item.database
    database._require_operation_lock()
    database.require_owned(name)
    config = database.config
    url = (
        f"postgresql://{quote(config['owner_user'], safe='')}:"
        f"{quote(config['owner_password'], safe='')}@127.0.0.1:{database.port}/{name}"
    )
    environment = dict(
        os.environ,
        DATABASE_URL=url,
        DB_SSLMODE="disable",
        PRAXIMESH_MODE="local",
        JUDGE_ENABLED="0",
        JUDGE_GPU_DEVICES="",
        PRAXIMESH_SCHEMA_FIXTURE_DATABASE=name,
        PRAXIMESH_SCHEMA_FIXTURE_MARKER=database._marker("database"),
    )
    arguments = (
        ["-m", "tests.deploy_schema_fixture"]
        if baseline
        else ["manage.py", "migrate", "--noinput", "--verbosity=0"]
    )
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if baseline else None


def project_original_rows(database, name, original, framework_ids):
    """Compare every original column/row, allowing only new framework identities."""
    result = {}
    with database.connection(name) as session:
        session.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table, info in original["tables"].items():
            condition = sql.SQL(" WHERE id=ANY(%s)") if table in framework_ids else sql.SQL("")
            query = sql.SQL("SELECT {} FROM {}{} ORDER BY {}").format(
                sql.SQL(",").join(sql.Identifier(column[0]) for column in info["columns"]),
                sql.Identifier(table),
                condition,
                sql.SQL(",").join(sql.Identifier(key[0]) for key in info["primary_key"]),
            )
            params = (framework_ids[table],) if table in framework_ids else None
            digest = hashlib.sha256()
            size = 0
            with session.cursor().copy(
                sql.SQL("COPY ({}) TO STDOUT (FORMAT BINARY)").format(query), params
            ) as stream:
                for block in stream:
                    size += len(block)
                    digest.update(block)
            result[table] = {"bytes": size, "sha256": digest.hexdigest()}
    return result


@pytest.fixture
def upgraded_f(owned_pg, tmp_path):
    item, database = owned_pg, owned_pg.database
    target = item.new_name()
    with database.operation_lock():
        seeded = command(item, item.name, baseline=True)
        database.grant(item.name)
    with item.runtime(item.name, "web") as web:
        web.execute("UPDATE core_draft SET source=%s", ("Private write through old Web role",))
        assert web.execute("SELECT count(*) FROM showcase_sharedresult").fetchone() == (2,)
    with item.runtime(item.name, "worker") as worker:
        assert worker.execute("SELECT count(*) FROM core_submission").fetchone() == (4,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT username FROM auth_user")
    with database.operation_lock():
        database.freeze(item.name)
        original = database._signature(item.name)
        assert "core_gpudevice" not in original["tables"]
        with database.connection(item.name) as session:
            framework_ids = {
                table: [
                    row[0]
                    for row in session.execute(
                        sql.SQL("SELECT id FROM {} ORDER BY id").format(sql.Identifier(table))
                    ).fetchall()
                ]
                for table in FRAMEWORK_TABLES
            }
        backup = database.backup_restore(item.name, target, tmp_path / "f-schema.dump")
        assert backup["tables"] == len(original["tables"])
        assert database._signature(target) == original
        command(item, target)
        database._require_frozen(item.name)
        database._require_frozen(target)
        assert database._signature(item.name) == original
        upgraded = database._signature(target)
        assert set(upgraded["tables"]) - set(original["tables"]) == {"core_gpudevice"}
        projected = project_original_rows(database, target, original, framework_ids)
        assert projected == {
            table: {key: value[key] for key in ("bytes", "sha256")}
            for table, value in original["tables"].items()
        }
        for table, info in original["tables"].items():
            assert upgraded["tables"][table]["columns"][: len(info["columns"])] == info["columns"]
            if table not in FRAMEWORK_TABLES:
                assert upgraded["tables"][table]["rows"] == info["rows"]
        for sequence, info in original["sequences"].items():
            after = upgraded["sequences"][sequence]
            assert after["definition"] == info["definition"]
            if sequence.removesuffix("_id_seq") in FRAMEWORK_TABLES:
                assert after["state"][0] >= info["state"][0]
            else:
                assert after["state"] == info["state"]
        with database.connection(target) as session:
            applied = set(session.execute("SELECT app,name FROM django_migrations").fetchall())
            assert applied - {tuple(node) for node in seeded["migrations"]} == ADDED
            assert session.execute("SELECT count(*) FROM core_gpudevice").fetchone() == (0,)
            assert session.execute(
                "SELECT metric_kind,is_public,revision FROM showcase_sharedresult ORDER BY is_public"
            ).fetchall() == [("sequential-v1", False, 3), ("sequential-v1", True, 3)]
        database.grant(target)
    return SimpleNamespace(
        item=item, target=target, seeded=seeded, original=original, backup=backup
    )


def test_f_copy_upgrade_preserves_private_rows_and_old_writer_defaults(upgraded_f):
    state = upgraded_f
    item = state.item
    with item.runtime(state.target, "web") as web:
        # The old INSERT omits the new field and must retain its old public scope.
        row = web.execute(
            "INSERT INTO showcase_sharedresult "
            "(id,is_public,revision,created_at,updated_at,submission_id) "
            "VALUES (%s,false,1,now(),now(),%s) RETURNING metric_kind,is_public",
            (uuid.uuid4(), state.seeded["jobs"]["released"]),
        ).fetchone()
        assert row == ("sequential-v1", False)
        with pytest.raises(psycopg.errors.CheckViolation):
            web.execute("UPDATE showcase_sharedresult SET metric_kind='unknown'")
        assert web.execute("SELECT count(*) FROM core_problem").fetchone() == (1,)
        assert web.execute("SELECT count(*) FROM core_problemversion").fetchone() == (2,)
    with item.database.operation_lock():
        item.database.freeze(state.target)
        item.database.thaw(state.target)
        item.database._require_frozen(item.name)
        assert item.database._signature(item.name) == state.original


def test_f_upgrade_preserves_cpu_budget_shapes_and_rejects_invalid_updates(upgraded_f):
    state = upgraded_f
    with state.item.runtime(state.target, "worker") as worker:
        states = worker.execute(
            "SELECT budget_state,budget_policy,budget_units,execution_started_at IS NOT NULL "
            "FROM core_submission ORDER BY budget_state"
        ).fetchall()
        assert states == [
            ("consumed", "cpu-invocations-v1", 3, True),
            ("legacy", "", 0, False),
            ("released", "cpu-invocations-v1", 3, False),
            ("reserved", "cpu-invocations-v1", 3, False),
        ]
        for clause in ("budget_policy='unknown'", "budget_units=0", "budget_day=NULL"):
            with pytest.raises(psycopg.errors.CheckViolation):
                worker.execute(
                    sql.SQL("UPDATE core_submission SET {} WHERE budget_state='reserved'").format(
                        sql.SQL(clause)
                    )
                )
        with pytest.raises(psycopg.errors.CheckViolation):
            worker.execute(
                "UPDATE core_submission SET execution_started_at=NULL WHERE budget_state='consumed'"
            )


def test_f_upgrade_new_gpu_table_keeps_web_read_only_and_worker_least_privilege(upgraded_f):
    state = upgraded_f
    with state.item.runtime(state.target, "worker") as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT source FROM core_draft")
        worker.execute(
            "INSERT INTO core_gpudevice "
            "(id,state,worker_id,owner_token,fingerprint,environment) "
            "VALUES ('synthetic-schema-device','quarantined','synthetic-worker',%s,%s,'{}')",
            (uuid.uuid4(), "a" * 64),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("DELETE FROM core_gpudevice")
    with state.item.runtime(state.target, "web") as web:
        assert web.execute(
            "SELECT state,environment,checked_at,job_id,reservation_token FROM core_gpudevice"
        ).fetchall() == [("quarantined", {}, None, None, None)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web.execute("UPDATE core_gpudevice SET state='ready'")
