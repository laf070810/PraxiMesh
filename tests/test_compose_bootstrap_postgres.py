"""Real PostgreSQL bootstrap/retry and least privileges; no Compose or GPU proof.

The owned regression container supplies the TCP endpoint. Only that transport
address is adapted; commands, full migrations, content and journal use real SQL.
"""

import copy
import multiprocessing
import os
import select
import signal
import uuid
from types import SimpleNamespace

import pytest
from psycopg import sql

from core.compose_bootstrap import Bootstrap, configuration_digest
from core.compose_bootstrap_state import initialize
from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError
from tests.test_deploy_database_postgres import owned_pg as owned_pg

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def installation(owned_pg, monkeypatch, tmp_path, request):
    original = owned_pg.database
    name = owned_pg.new_name()
    config = dict(original.config, container=f"px-{original.instance}-postgres")
    spec = {
        "schema": 1,
        "instance": original.instance,
        "site_id": original.site_id,
        "generation": name[-12:],
        "source_id": "a" * 64,
        "release_sha256": "b" * 64,
        "initial_content": getattr(request, "param", True),
        "database": config,
        "application_secret": "private-bootstrap-test-secret-" + uuid.uuid4().hex,
        "wasi_sha256": "c" * 64,
    }
    database = DeploymentDatabase.for_compose_bootstrap(config)
    database.host, database.port = original.host, original.port
    url = database.url
    monkeypatch.setattr(
        database,
        "url",
        lambda name, role: url(name, role).replace(
            "@" + config["container"] + ":5432/", f"@127.0.0.1:{original.port}/"
        ),
    )
    directory = tmp_path / "bootstrap-state"
    initialize(directory, configuration_digest(spec))
    bootstrap = Bootstrap(spec, database, state_directory=directory)
    try:
        yield SimpleNamespace(
            spec=spec,
            database=database,
            bootstrap=bootstrap,
            name=name,
            runtime=lambda role: owned_pg.runtime(name, role),
            directory=directory,
        )
    finally:
        with original._connect("postgres") as connection:
            marker = connection.execute(
                "SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=%s",
                (bootstrap.schema,),
            ).fetchone()
            if marker is not None:
                assert marker == (bootstrap.marker,)
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(bootstrap.schema))
                )


def private_user(item):
    with item.runtime("web") as connection:
        return connection.execute(
            "INSERT INTO auth_user (username,password,first_name,last_name,email,is_staff,"
            "is_active,is_superuser,date_joined) VALUES ('bootstrap-private-user','!',"
            "'private name','','',false,true,false,now()) RETURNING id"
        ).fetchone()[0]


def test_real_new_database_full_content_permissions_and_same_version_reopen(installation):
    item = installation
    assert item.bootstrap.run() == {"initialized": True, "database": item.name}
    with item.runtime("web") as connection:
        assert connection.execute("SELECT count(*) FROM core_problem").fetchone() == (26,)
        assert connection.execute("SELECT count(*) FROM core_gpudevice").fetchone() == (0,)
    user = private_user(item)
    assert item.bootstrap.run() == {"initialized": False, "database": item.name}
    with item.runtime("web") as connection:
        assert connection.execute(
            "SELECT first_name FROM auth_user WHERE id=%s", (user,)
        ).fetchone() == ("private name",)
    with item.database.operation_lock():
        item.database._verify_permissions(item.name, connected=True)
        assert item.bootstrap.journal() == ("ready", 4)


@pytest.mark.parametrize("installation", [False], indirect=True)
def test_initial_catalog_requires_explicit_selection(installation):
    item = installation
    item.bootstrap.run()
    with item.runtime("web") as connection:
        assert connection.execute("SELECT count(*) FROM core_problem").fetchone() == (0,)


@pytest.mark.parametrize(
    "command", ["migrate", "content_import", "project_import", "challenge_import"]
)
def test_lost_initialization_acknowledgement_resumes_original_closed_database(
    installation, monkeypatch, command
):
    item = installation
    manage = item.bootstrap.manage

    def interrupted(arguments):
        manage(arguments)
        if arguments[0] == command:
            raise RuntimeError("Injected response loss after actual command completion")

    monkeypatch.setattr(item.bootstrap, "manage", interrupted)
    with pytest.raises(RuntimeError, match="Injected response loss"):
        item.bootstrap.run()
    with item.database.operation_lock():
        item.database._require_frozen(item.name)
    monkeypatch.setattr(item.bootstrap, "manage", manage)
    item.bootstrap.run()
    with item.runtime("web") as connection:
        assert connection.execute("SELECT count(*) FROM core_problem").fetchone() == (26,)


def test_lost_final_acknowledgement_keeps_live_connections_and_new_writes(
    installation, monkeypatch
):
    item = installation
    save = item.bootstrap.save

    def lost_ack(phase, completed):
        if phase == "ready":
            raise RuntimeError("Injected acknowledgement loss after grants")
        return save(phase, completed)

    monkeypatch.setattr(item.bootstrap, "save", lost_ack)
    with pytest.raises(RuntimeError, match="after grants"):
        item.bootstrap.run()
    user = private_user(item)
    monkeypatch.setattr(item.bootstrap, "save", save)
    with item.runtime("web") as connection:
        pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        item.bootstrap.run()
        assert connection.execute("SELECT pg_backend_pid()").fetchone() == (pid,)
        assert connection.execute(
            "SELECT first_name FROM auth_user WHERE id=%s", (user,)
        ).fetchone() == ("private name",)


@pytest.mark.parametrize("field", ["source_id", "release_sha256", "generation", "initial_content"])
def test_new_version_or_changed_configuration_cannot_migrate_existing_installation(
    installation, field, monkeypatch
):
    item = installation
    item.bootstrap.run()
    changed = copy.deepcopy(item.spec)
    changed[field] = (
        not changed[field] if type(changed[field]) is bool else "d" * len(changed[field])
    )
    other = Bootstrap(changed, item.database, state_directory=item.directory)
    monkeypatch.setattr(
        other, "manage", lambda _: pytest.fail("Changed configuration reached migration")
    )
    with pytest.raises(DeploymentDatabaseError, match="configuration"):
        other.run()
    item.database._verify_permissions(item.name, connected=True)


def test_completed_installation_never_recreates_a_missing_database(installation):
    item = installation
    item.bootstrap.run()
    with item.database._connect("postgres", autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(item.name)))
    with pytest.raises(DeploymentDatabaseError, match="missing"):
        item.bootstrap.run()
    assert not item.database.owned_exists(item.name)


@pytest.mark.parametrize("remove_application_database", [False, True])
def test_missing_postgres_metadata_cannot_turn_bound_installation_into_new_site(
    installation, remove_application_database
):
    item = installation
    item.bootstrap.run()
    with item.database._connect("postgres", autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(item.bootstrap.schema))
        )
        if remove_application_database:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(item.name)))
    with pytest.raises(DeploymentDatabaseError, match="metadata is missing"):
        item.bootstrap.run()
    assert item.database.owned_exists(item.name) is not remove_application_database
    with item.database._connect("postgres") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (item.bootstrap.schema,)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("checkpoint", ["migration-complete", "grants-complete"])
def test_actual_bootstrap_process_kill_resumes_original_database(installation, checkpoint):
    item = installation
    receiving, sending = os.pipe()

    def child():
        os.close(receiving)

        def pause():
            os.write(sending, b"original-bootstrap-checkpoint")
            os.kill(os.getpid(), signal.SIGSTOP)
            raise AssertionError("Paused original child must be killed, never resumed")

        if checkpoint == "migration-complete":
            manage = item.bootstrap.manage

            def interrupted(arguments):
                manage(arguments)
                if arguments[0] == "migrate":
                    pause()

            item.bootstrap.manage = interrupted
        else:
            save = item.bootstrap.save

            def interrupted(phase, completed):
                if phase == "ready":
                    pause()
                return save(phase, completed)

            item.bootstrap.save = interrupted
        item.bootstrap.run()

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    os.close(sending)
    try:
        assert select.select([receiving], [], [], 45)[0], "Original bootstrap missed its checkpoint"
        assert os.read(receiving, 128) == b"original-bootstrap-checkpoint"
        assert process.is_alive()
        user = private_user(item) if checkpoint == "grants-complete" else None
        # This unreaped child belongs to this Process object; its PID cannot be reused.
        process.kill()
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
        item.bootstrap.run()
        with item.runtime("web") as connection:
            assert connection.execute("SELECT count(*) FROM core_problem").fetchone() == (26,)
            if user is not None:
                assert connection.execute(
                    "SELECT first_name FROM auth_user WHERE id=%s", (user,)
                ).fetchone() == ("private name",)
    finally:
        os.close(receiving)
        if process.is_alive():
            process.kill()
            process.join(10)


def test_existing_owned_database_without_bootstrap_record_is_not_adopted(installation):
    item = installation
    with item.database.operation_lock():
        item.database.ensure_roles()
        item.database.create(item.name)
    with pytest.raises(DeploymentDatabaseError, match="no bootstrap journal"):
        item.bootstrap.run()
    with item.database._connect("postgres") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (item.bootstrap.schema,)
            ).fetchone()
            is None
        )
