"""Real owned PostgreSQL/Docker copy and current-schema permissions; no GPU.

Only scripts/postgres_tests.py supplies the dedicated container. Every extra
database and role has a random namespace and the coordinator's actual marker.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError
from scripts.release import docker

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def owned_pg():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the owned PostgreSQL container runner")
    container = os.environ.get("PRAXIMESH_TEST_POSTGRES_CONTAINER", "")
    if not re.fullmatch(r"praximesh-regression-[a-f0-9]{12}", container):
        pytest.skip("Run scripts/postgres_tests.py to supply its owned PostgreSQL container")
    parameters = connection.get_connection_params()
    assert parameters["host"] == "127.0.0.1"
    labels = json.loads(
        docker(
            "inspect", "--format", "{{json .Config.Labels}}", container, capture_output=True
        ).stdout
    )
    assert labels["org.praximesh.acceptance"] == container.removeprefix("praximesh-regression-")
    database = DeploymentDatabase(
        {
            "instance": "gr" + uuid.uuid4().hex[:12],
            "site_id": uuid.uuid4().hex,
            "container": container,
            "owner_user": parameters["user"],
            "owner_password": parameters["password"],
            "web_password": secrets.token_urlsafe(32),
            "worker_password": secrets.token_urlsafe(32),
        }
    )
    assert database.port == int(parameters["port"])
    names = []

    def new_name():
        name = database.name(uuid.uuid4().hex[:12])
        names.append(name)
        return name

    def runtime(name, role):
        assert name in names
        return psycopg.connect(
            host="127.0.0.1",
            port=database.port,
            dbname=name,
            user=database.roles[role],
            password=database.config[role + "_password"],
            connect_timeout=5,
            sslmode="disable",
            autocommit=True,
        )

    try:
        with database.operation_lock():
            database.ensure_roles()
            name = new_name()
            database.create(name)
        yield SimpleNamespace(database=database, name=name, new_name=new_name, runtime=runtime)
    finally:
        # Never drop by prefix or with a missing/mismatched ownership marker.
        with database.operation_lock():
            for name in reversed(names):
                if database.owned_exists(name):
                    with database._connect("postgres", autocommit=True) as session:
                        session.execute(
                            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                        )
            with database._connect("postgres") as session:
                for role, identity in database.roles.items():
                    marker = session.execute(
                        "SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",
                        (identity,),
                    ).fetchone()
                    if marker is not None:
                        assert marker == (database._marker(role),)
                        session.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(identity)))
                assert not session.execute(
                    "SELECT 1 FROM pg_database WHERE datname=ANY(%s)", (names,)
                ).fetchone()


@pytest.fixture
def migrated_pg(owned_pg):
    item, database = owned_pg, owned_pg.database
    url = (
        f"postgresql://{quote(database.config['owner_user'], safe='')}:"
        f"{quote(database.config['owner_password'], safe='')}@127.0.0.1:{database.port}/{item.name}"
        "?sslmode=disable"
    )
    # This fresh owned DB receives the current real migration history. No content
    # import, service/worker startup or existing developer database is involved.
    environment = dict(
        os.environ,
        DATABASE_URL=url,
        DB_SSLMODE="disable",
        PRAXIMESH_MODE="local",
        JUDGE_ENABLED="0",
        JUDGE_GPU_DEVICES="",
    )
    with database.operation_lock():
        subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=60,
        )
        database.grant(item.name)
    with item.runtime(item.name, "web") as web:
        item.user_id = web.execute(
            "INSERT INTO auth_user (username,password,first_name,last_name,email,is_staff,"
            "is_active,is_superuser,date_joined) VALUES (%s,'!',%s,'','',false,true,false,now()) "
            "RETURNING id",
            ("owned-restore-check", "private-original"),
        ).fetchone()[0]
    return item


def test_real_owned_exists_distinguishes_absence_without_creating_database(owned_pg):
    item, database = owned_pg, owned_pg.database
    missing = item.new_name()
    assert database.owned_exists(item.name)
    assert not database.owned_exists(missing)
    with pytest.raises(DeploymentDatabaseError, match="missing"):
        database.require_owned(missing)
    with database._connect("postgres") as session:
        assert not session.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (missing,)
        ).fetchone()


@pytest.mark.parametrize("defect", ["unmarked", "other-site", "other-owner"])
def test_real_owned_exists_refuses_changed_identity_without_adopting_it(owned_pg, defect):
    item, database = owned_pg, owned_pg.database
    with database._connect("postgres", autocommit=True) as session:
        oid = session.execute(
            "SELECT oid FROM pg_database WHERE datname=%s", (item.name,)
        ).fetchone()[0]
        if defect == "other-owner":
            statement = sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(item.name), sql.Identifier(database.roles["worker"])
            )
        else:
            statement = sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(item.name),
                sql.Literal(None if defect == "unmarked" else "another-site"),
            )
        session.execute(statement)
        changed = session.execute(
            "SELECT datdba,shobj_description(oid,'pg_database') FROM pg_database WHERE oid=%s",
            (oid,),
        ).fetchone()
    try:
        with pytest.raises(DeploymentDatabaseError, match="owner and marker"):
            database.owned_exists(item.name)
        with database._connect("postgres") as session:
            assert (
                session.execute(
                    "SELECT datdba,shobj_description(oid,'pg_database') FROM pg_database WHERE oid=%s",
                    (oid,),
                ).fetchone()
                == changed
            )
    finally:
        # Undo only this test's own deliberate identity change, after checking
        # the original OID and exact modified values, so fixture cleanup is safe.
        with database._connect("postgres", autocommit=True) as session:
            assert session.execute(
                "SELECT oid,datdba,shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                (item.name,),
            ).fetchone() == (oid, *changed)
            session.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(item.name), sql.Identifier(database.config["owner_user"])
                )
            )
            session.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(item.name), sql.Literal(database._marker("database"))
                )
            )


def test_current_schema_freeze_and_thaw_preserve_private_data_and_runtime_permissions(migrated_pg):
    item, database = migrated_pg, migrated_pg.database
    with item.runtime(item.name, "worker") as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT username FROM auth_user")
        assert worker.execute("SELECT count(*) FROM core_workerhealth").fetchone() == (0,)
    with database.operation_lock():
        database.freeze(item.name)
        database._require_frozen(item.name)
        for role in ("web", "worker"):
            with pytest.raises(psycopg.OperationalError):
                item.runtime(item.name, role)
        database.thaw(item.name)
        database.thaw(item.name)
    with item.runtime(item.name, "web") as web:
        assert web.execute(
            "SELECT first_name FROM auth_user WHERE id=%s", (item.user_id,)
        ).fetchone() == ("private-original",)
    with item.runtime(item.name, "worker") as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT username FROM auth_user")


@pytest.mark.parametrize(
    "role,table,column,privilege",
    [
        ("worker", "auth_user", "username", "SELECT"),
        ("web", "core_gpudevice", "state", "UPDATE"),
        ("web", "auth_user", "username", "SELECT WITH GRANT OPTION"),
    ],
)
def test_real_column_or_grant_option_change_prevents_thaw(
    migrated_pg, role, table, column, privilege
):
    item, database = migrated_pg, migrated_pg.database
    with database.operation_lock():
        database.freeze(item.name)
        with database.connection(item.name) as session:
            base = privilege.removesuffix(" WITH GRANT OPTION")
            session.execute(
                sql.SQL("GRANT {} ({}) ON {} TO {}{}").format(
                    sql.SQL(base),
                    sql.Identifier(column),
                    sql.Identifier(table),
                    sql.Identifier(database.roles[role]),
                    sql.SQL(
                        " WITH GRANT OPTION" if privilege.endswith(" WITH GRANT OPTION") else ""
                    ),
                )
            )
        with pytest.raises(DeploymentDatabaseError, match="privileges"):
            database.thaw(item.name)
        database._require_frozen(item.name)
    for identity in ("web", "worker"):
        with pytest.raises(psycopg.OperationalError):
            item.runtime(item.name, identity)


def test_real_dump_restore_and_target_recovery_preserve_rows_sequences_and_new_writes(
    migrated_pg, tmp_path
):
    item, database = migrated_pg, migrated_pg.database
    target = item.new_name()
    with database.operation_lock():
        database.freeze(item.name)
        source_signature = database._signature(item.name)
        backup = tmp_path / "owned-original.dump"
        database.backup_restore(item.name, target, backup)
        assert backup.stat().st_size > 0
        database._require_frozen(item.name)
        database._require_frozen(target)
        assert database._signature(target) == source_signature
        database.grant(target)
    with item.runtime(target, "web") as web:
        assert web.execute("SELECT id,first_name FROM auth_user").fetchall() == [
            (item.user_id, "private-original")
        ]
        web.execute(
            "UPDATE auth_user SET first_name=%s WHERE id=%s", ("write-after-exposure", item.user_id)
        )
    with database.operation_lock():
        database.freeze(target)
        database.thaw(target)
    with item.runtime(target, "web") as web:
        assert web.execute(
            "SELECT first_name FROM auth_user WHERE id=%s", (item.user_id,)
        ).fetchone() == ("write-after-exposure",)
    with database.connection(item.name) as source:
        assert source.execute(
            "SELECT first_name FROM auth_user WHERE id=%s", (item.user_id,)
        ).fetchone() == ("private-original",)
