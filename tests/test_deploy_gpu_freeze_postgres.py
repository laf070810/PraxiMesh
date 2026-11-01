"""Real CONNECT/session/row-lock checks in a separate, owned temporary database.

No Docker or GPU calls. The PostgreSQL runner must supply an administrative
test connection; a minimal freeze schema is created, not a deployed application.
"""

import uuid
from types import SimpleNamespace

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def frozen_pg():
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL owned database, roles, CONNECT and session control")
    parameters = connection.get_connection_params()
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        owner = cursor.fetchone()[0]
    database = object.__new__(DeploymentDatabase)
    database.instance = "gf" + uuid.uuid4().hex[:12]
    database.site_id = uuid.uuid4().hex
    database.roles = {role: f"px_{database.instance}_{role}" for role in ("web", "worker")}
    database.config = {
        "owner_user": owner,
        "web_password": uuid.uuid4().hex,
        "worker_password": uuid.uuid4().hex,
    }
    database._operation_connection = None
    database._lock_key = uuid.uuid4().int % (2**63)
    name, token = database.name(uuid.uuid4().hex[:12]), str(uuid.uuid4())

    def connect(target, *, autocommit=False):
        return psycopg.connect(**(parameters | {"dbname": target}), autocommit=autocommit)

    def runtime(role):
        return psycopg.connect(
            **(
                parameters
                | {
                    "dbname": name,
                    "user": database.roles[role],
                    "password": database.config[role + "_password"],
                    "connect_timeout": 5,
                }
            ),
            autocommit=True,
        )

    database._connect = connect
    created = False
    try:
        with database.operation_lock():
            database.ensure_roles()
            database.create(name)
            created = True
            with database.connection(name) as session:
                for statement in (
                    "CREATE TABLE django_migrations (app text, name text)",
                    "CREATE TABLE core_queuelock (id int PRIMARY KEY, pause_token uuid, paused_at timestamptz)",
                    "CREATE TABLE core_gpudevice (id text PRIMARY KEY, job_id uuid, reservation_token uuid)",
                    "CREATE TABLE core_submission (status text)",
                    "CREATE TABLE core_problemversion (id int)",
                    "CREATE TABLE core_workerhealth (id int)",
                    "INSERT INTO django_migrations VALUES ('core', '0004_gpu_resources')",
                ):
                    session.execute(statement)
                session.execute("INSERT INTO core_queuelock VALUES (1, %s, now())", (token,))
                session.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                        sql.Identifier(name), *map(sql.Identifier, database.roles.values())
                    )
                )
        yield SimpleNamespace(database=database, name=name, token=token, runtime=runtime)
    finally:
        # Only this fixture's unique, marked database and matching roles are removed.
        if created:
            database.require_owned(name)
            with connect("postgres", autocommit=True) as session:
                session.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )
        with connect("postgres") as session:
            for role, identity in database.roles.items():
                marker = session.execute(
                    "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",
                    (identity,),
                ).fetchone()
                if marker is not None:
                    assert marker == (database._marker(role),)
                    session.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(identity)))


def test_real_freeze_terminates_runtime_and_refuses_reconnect_then_retries(frozen_pg):
    item, database = frozen_pg, frozen_pg.database
    with item.runtime("web") as web, item.runtime("worker") as worker:
        assert web.execute("SELECT 1").fetchone() == worker.execute("SELECT 1").fetchone() == (1,)
        with database.operation_lock():
            database.freeze_gpu(item.name, item.token)
            database.freeze_gpu(item.name, item.token)
            database._require_frozen(item.name)
        for session in (web, worker):
            with pytest.raises(psycopg.OperationalError):
                session.execute("SELECT 1")
    for role in ("web", "worker"):
        with pytest.raises(psycopg.OperationalError):
            item.runtime(role)
    with database.connection(item.name) as session:
        assert (
            str(session.execute("SELECT pause_token FROM core_queuelock").fetchone()[0])
            == item.token
        )


def test_real_freeze_refuses_another_owner_connection_without_revoking_runtime(frozen_pg):
    item, database = frozen_pg, frozen_pg.database
    with database.connection(item.name):
        with (
            database.operation_lock(),
            pytest.raises(DeploymentDatabaseError, match="Unexpected database sessions"),
        ):
            database.freeze_gpu(item.name, item.token)
    with item.runtime("web") as web:
        assert web.execute("SELECT 1").fetchone() == (1,)


def test_real_orphan_reservation_prevents_freeze_and_is_preserved(frozen_pg):
    item, database = frozen_pg, frozen_pg.database
    job, reservation = uuid.uuid4(), uuid.uuid4()
    with database.connection(item.name) as session:
        session.execute(
            "INSERT INTO core_gpudevice VALUES ('synthetic-unused', %s, %s)", (job, reservation)
        )
    with database.operation_lock(), pytest.raises(DeploymentDatabaseError, match="fully drained"):
        database.freeze_gpu(item.name, item.token)
    with database.connection(item.name) as session:
        assert session.execute(
            "SELECT job_id, reservation_token FROM core_gpudevice"
        ).fetchone() == (job, reservation)
    with item.runtime("worker") as worker:
        assert worker.execute("SELECT 1").fetchone() == (1,)


def test_real_freeze_retains_queue_and_device_locks_through_connect_revoke(frozen_pg, monkeypatch):
    item, database = frozen_pg, frozen_pg.database
    original = database._freeze_runtime

    def checked(name, *, guard_pid):
        with database._connect(name, autocommit=True) as probe:
            assert probe.info.backend_pid != guard_pid
            probe.execute("SET lock_timeout='150ms'")
            for statement in (
                "UPDATE core_queuelock SET pause_token=NULL",
                "INSERT INTO core_gpudevice VALUES ('synthetic-unused', NULL, NULL)",
            ):
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    probe.execute(statement)
        original(name, guard_pid=guard_pid)
        database._require_frozen(name, guard_pid=guard_pid)

    monkeypatch.setattr(database, "_freeze_runtime", checked)
    with database.operation_lock():
        database.freeze_gpu(item.name, item.token)
        database._require_frozen(item.name)


def test_real_post_revoke_failure_keeps_database_closed_for_retry(frozen_pg, monkeypatch):
    item, database = frozen_pg, frozen_pg.database
    original = database._freeze_runtime

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lost freeze acknowledgment")

    monkeypatch.setattr(database, "_freeze_runtime", failed)
    with database.operation_lock(), pytest.raises(RuntimeError, match="acknowledgment"):
        database.freeze_gpu(item.name, item.token)
    with pytest.raises(psycopg.OperationalError):
        item.runtime("web")
    monkeypatch.setattr(database, "_freeze_runtime", original)
    with database.operation_lock():
        database.freeze_gpu(item.name, item.token)
