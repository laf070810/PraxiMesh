"""Deployment DB trust boundaries; no simulated migration/restore success."""

import json
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
import pytest

from scripts import deploy_database
from scripts.deploy_database import (
    WEB_READ,
    WEB_WRITE,
    WORKER_PRIVILEGES,
    DeploymentDatabase,
    DeploymentDatabaseError,
    loopback_port,
    safe_errors,
    write_private_backup,
)


@pytest.fixture
def configuration():
    return {
        "instance": "example1",
        "site_id": "a" * 32,
        "container": "postgres-example",
        "owner_user": "postgres",
        "owner_password": "private-owner-password",
        "web_password": "private-web:p@ss/word?#%",
        "worker_password": "private-worker-password",
    }


@pytest.fixture
def database(monkeypatch, configuration):
    def inspect(*arguments):
        return (
            "127.0.0.1:15432\n"
            if arguments[0] == "port"
            else json.dumps({"name": "/postgres-example", "running": True})
        )

    monkeypatch.setattr(deploy_database, "docker_output", inspect)
    return DeploymentDatabase(configuration)


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0:5432",
        "[::1]:5432",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:5432\n0.0.0.0:5432",
        "127.0.0.1:5432 extra",
    ],
)
def test_postgres_port_rejects_any_nonexclusive_loopback_binding(value):
    with pytest.raises(DeploymentDatabaseError):
        loopback_port(value)


def test_postgres_port_accepts_exact_mapping():
    assert loopback_port("127.0.0.1:15432\n") == 15432


@pytest.mark.parametrize(
    "key,value",
    [
        ("instance", "other; DROP DATABASE postgres"),
        ("instance", "UPPER"),
        ("instance", "a" * 21),
        ("site_id", "A" * 32),
        ("site_id", "a" * 31),
        ("container", "--help"),
        ("container", "other/container"),
        ("owner_user", "postgres;--"),
        ("owner_user", "px_example1_web"),
        ("owner_password", "secret\0tail"),
    ],
)
def test_invalid_identifiers_are_rejected_before_any_external_access(
    monkeypatch, configuration, key, value
):
    def forbidden(*args):
        pytest.fail("Invalid configuration reached Docker")

    monkeypatch.setattr(deploy_database, "docker_output", forbidden)
    configuration[key] = value
    with pytest.raises(DeploymentDatabaseError):
        DeploymentDatabase(configuration)


@pytest.mark.parametrize(
    "inspection",
    [{"name": "/different", "running": True}, {"name": "/postgres-example", "running": False}],
)
def test_container_alias_or_stopped_container_is_rejected(monkeypatch, configuration, inspection):
    monkeypatch.setattr(deploy_database, "docker_output", lambda *args: json.dumps(inspection))
    with pytest.raises(DeploymentDatabaseError):
        DeploymentDatabase(configuration)


def test_generation_and_instance_boundaries(database):
    assert database.name("a0123456789f") == "px_example1_a0123456789f"
    for generation in ["A0123456789F", "123", "a" * 13, "../aaaaaaaaaaaa"]:
        with pytest.raises(DeploymentDatabaseError):
            database.name(generation)
    for name in ["postgres", "px_other_a0123456789f", "px_example1_a0123456789f;--"]:
        with pytest.raises(DeploymentDatabaseError):
            database._validate_name(name)


def test_django_url_preserves_quoted_credentials_and_container_boundary(database, configuration):
    result = urlsplit(database.url(database.name("a0123456789f"), "web"))
    assert result.hostname == configuration["container"] and result.port == 5432
    assert unquote(result.password) == configuration["web_password"]
    assert result.username == "px_example1_web"
    assert result.path == "/px_example1_a0123456789f"
    assert parse_qs(result.query) == {"sslmode": ["disable"]}
    with pytest.raises(DeploymentDatabaseError):
        database.url("postgres", "web")
    with pytest.raises(DeploymentDatabaseError):
        database.url(database.name("a0123456789f"), "unknown")


@pytest.mark.parametrize(
    "position,value",
    [
        (0, None),
        (0, "another-instance"),
        (1, False),
        (2, True),
        (3, True),
        (4, True),
        (5, True),
        (6, True),
        (7, True),
        (8, True),
    ],
)
def test_existing_role_requires_marker_and_no_extra_privileges(database, position, value):
    row = [database._marker("web"), True, False, False, False, False, False, False, False]
    row[position] = value
    with pytest.raises(DeploymentDatabaseError):
        database._check_role(tuple(row), "web")


def test_runtime_permissions_separate_private_accounts_content_and_health():
    assert not set(WEB_READ) & set(WEB_WRITE)
    assert "core_workerhealth" in WEB_READ and "core_problemversion" in WEB_READ
    assert "auth_user" in WEB_WRITE and "core_draft" in WEB_WRITE
    assert "django_migrations" not in WEB_READ + WEB_WRITE
    assert WORKER_PRIVILEGES == {
        "core_submission": "SELECT, UPDATE",
        "core_problemversion": "SELECT",
        "core_workerhealth": "SELECT, INSERT, UPDATE",
    }
    assert "learning_note" in WEB_WRITE and "community_report" in WEB_WRITE
    assert not any(name.startswith(("learning_", "community_")) for name in WORKER_PRIVILEGES)


@pytest.mark.parametrize(
    "installed", [(), (("learning", "0001_initial"),), tuple(deploy_database.FEATURE_TABLES)]
)
def test_web_role_supports_old_and_migrated_schema(installed):
    tables = set(deploy_database.BASE_WEB_WRITE)
    for migration in installed:
        tables.update(deploy_database.FEATURE_TABLES[migration])
    tables.add("unrelated_private_table")

    class Connection:
        def execute(self, statement):
            rows = installed if "django_migrations" in statement else [(table,) for table in tables]
            return SimpleNamespace(fetchall=lambda: rows)

    granted = set(deploy_database.web_write_tables(Connection()))
    assert granted == tables - {"unrelated_private_table"}


@pytest.mark.parametrize("recorded", [True, False])
def test_web_role_fails_closed_when_feature_schema_disagrees_with_migrations(recorded):
    installed = [("learning", "0001_initial")] if recorded else []
    tables = [*deploy_database.BASE_WEB_WRITE, "learning_note"]

    class Connection:
        def execute(self, statement):
            rows = installed if "django_migrations" in statement else [(table,) for table in tables]
            return SimpleNamespace(fetchall=lambda: rows)

    with pytest.raises(DeploymentDatabaseError):
        deploy_database.web_write_tables(Connection())


@pytest.mark.parametrize("installed", [(), tuple(deploy_database.FEATURE_READ_TABLES)])
def test_translations_are_read_only_and_optional_for_old_generations(installed):
    tables = set(deploy_database.BASE_WEB_READ)
    for migration in installed:
        tables.update(deploy_database.FEATURE_READ_TABLES[migration])

    class Connection:
        def execute(self, statement):
            rows = installed if "django_migrations" in statement else [(table,) for table in tables]
            return SimpleNamespace(fetchall=lambda: rows)

    granted = set(deploy_database.web_read_tables(Connection()))
    assert granted == tables
    assert not granted.intersection(WEB_WRITE)
    assert not any(name.startswith("localization_") for name in WORKER_PRIVILEGES)


@pytest.mark.parametrize("recorded", [True, False])
def test_partial_translation_schema_fails_closed(recorded):
    installed = [("localization", "0001_initial")] if recorded else []
    tables = [*deploy_database.BASE_WEB_READ, "localization_knowledgesnapshot"]

    class Connection:
        def execute(self, statement):
            rows = installed if "django_migrations" in statement else [(table,) for table in tables]
            return SimpleNamespace(fetchall=lambda: rows)

    with pytest.raises(DeploymentDatabaseError):
        deploy_database.web_read_tables(Connection())


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        ("postgres", None),
        ("different-owner", "praximesh deployment v1 instance=example1 object=database"),
        ("postgres", "praximesh deployment v1 instance=other object=database"),
    ],
)
def test_missing_or_wrong_database_marker_never_reaches_mutation(database, monkeypatch, metadata):
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, parameters):
            statements.append(statement)
            return SimpleNamespace(fetchone=lambda: metadata)

    monkeypatch.setattr(database, "_connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(database, "_require_operation_lock", lambda: None)
    for operation in [database.grant, database.freeze, database.thaw, database.invalidate_health]:
        with pytest.raises(DeploymentDatabaseError):
            operation(database.name("a0123456789f"))
    assert statements and all(statement.startswith("SELECT ") for statement in statements)


def test_database_errors_do_not_repeat_private_server_diagnostics():
    @safe_errors
    def operation():
        raise psycopg.OperationalError("password=private-password row=private-source")

    with pytest.raises(DeploymentDatabaseError) as error:
        operation()
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_pg_commands_keep_credentials_out_of_arguments_and_discard_server_output(
    database, monkeypatch
):
    observed = []

    def run(arguments, **kwargs):
        observed.append((arguments, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(deploy_database.subprocess, "run", run)
    monkeypatch.setattr(database, "_require_operation_lock", lambda: None)
    monkeypatch.setattr(database, "require_owned", lambda name: None)
    terminated = []
    monkeypatch.setattr(
        database,
        "_terminate_copy",
        lambda name, application: terminated.append((name, application)),
    )
    with pytest.raises(DeploymentDatabaseError):
        database._pg_command("pg_restore", database.name("a0123456789f"))
    arguments, options = observed[0]
    assert all(
        password not in " ".join(arguments)
        for key, password in database.config.items()
        if key.endswith("password")
    )
    assert options["env"]["PGPASSWORD"] == database.config["owner_password"]
    assert "--single-transaction" in arguments and "--exit-on-error" in arguments
    assert "--no-owner" in arguments and "--no-acl" in arguments
    assert "--create" not in arguments and "--clean" not in arguments
    assert options["stderr"] == deploy_database.subprocess.DEVNULL
    assert ["timeout", "-s", "TERM", "-k", "5", "900"] == arguments[
        arguments.index("timeout") : arguments.index("timeout") + 6
    ]
    assert options["env"]["PGCONNECT_TIMEOUT"] == "5"
    assert terminated == [(database.name("a0123456789f"), options["env"]["PGAPPNAME"])]


@pytest.mark.parametrize("kind", ["existing", "symlink", "public-parent", "symlink-parent"])
def test_backup_never_overwrites_or_uses_public_or_redirected_paths(
    database, monkeypatch, tmp_path, kind
):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    backup = directory / "backup.dump"
    protected = tmp_path / "existing-data"
    protected.write_bytes(b"retain the existing bytes")
    if kind == "existing":
        backup.write_bytes(b"retain the existing backup")
    elif kind == "symlink":
        backup.symlink_to(protected)
    elif kind == "public-parent":
        directory.chmod(0o755)
    else:
        redirected = tmp_path / "redirected"
        redirected.symlink_to(directory, target_is_directory=True)
        backup = redirected / "backup.dump"
    # Only prerequisites are stubbed: this asserts rejection before pg_dump;
    # no backup/restore result is simulated or claimed as a successful restore.
    monkeypatch.setattr(database, "_require_frozen", lambda name: None)
    monkeypatch.setattr(database, "_require_operation_lock", lambda: None)
    monkeypatch.setattr(database, "_signature", lambda name: {})

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected backup destination reached PostgreSQL tooling")

    monkeypatch.setattr(database, "_pg_command", forbidden)
    with pytest.raises(DeploymentDatabaseError):
        database.backup_restore(database.name("a" * 12), database.name("b" * 12), backup)
    assert protected.read_bytes() == b"retain the existing bytes"
    if kind == "existing":
        assert backup.read_bytes() == b"retain the existing backup"


def test_unknown_database_sessions_block_freezing_before_any_revoke(database, monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args, **kwargs):
            pytest.fail("Unknown sessions must prevent any database mutation")

    monkeypatch.setattr(database, "require_owned", lambda name: None)
    monkeypatch.setattr(database, "_require_operation_lock", lambda: None)
    monkeypatch.setattr(database, "_require_roles", lambda: None)
    monkeypatch.setattr(database, "_connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(database, "_sessions", lambda *args: [(123, "unrelated-admin")])
    with pytest.raises(DeploymentDatabaseError, match="Unexpected database sessions"):
        database.freeze(database.name("a" * 12))


def test_cross_state_instance_has_same_lock_but_different_ownership(
    database, configuration, monkeypatch
):
    configuration["site_id"] = "b" * 32
    other = DeploymentDatabase(configuration)
    assert database._lock_key == other._lock_key
    assert database._marker("web") != other._marker("web")
    with pytest.raises(DeploymentDatabaseError):
        other._check_role(
            (database._marker("web"), True, False, False, False, False, False, False, False), "web"
        )
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, parameters):
            statements.append(statement)
            return SimpleNamespace(
                fetchone=lambda: (
                    database._marker("web"),
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                )
            )

    monkeypatch.setattr(other, "_connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(other, "_require_operation_lock", lambda: None)
    with pytest.raises(DeploymentDatabaseError):
        other.ensure_roles()
    assert statements and all(statement.startswith("SELECT ") for statement in statements)


def test_database_changes_require_a_live_operation_lock(database):
    for action in [
        database.create,
        database.grant,
        database.freeze,
        database.thaw,
        database.invalidate_health,
    ]:
        with pytest.raises(DeploymentDatabaseError, match="require operation_lock"):
            action(database.name("a" * 12))
    with pytest.raises(DeploymentDatabaseError, match="require operation_lock"):
        database.ensure_roles()


def test_contended_pg_advisory_lock_refuses_operation_without_wait(database, monkeypatch):
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, parameters):
            statements.append(statement)
            return SimpleNamespace(fetchone=lambda: (False,))

    monkeypatch.setattr(database, "_connect", lambda *args, **kwargs: Connection())
    with pytest.raises(DeploymentDatabaseError, match="Another coordinator"):
        with database.operation_lock():
            pytest.fail("A contended operation entered its body")
    assert statements == ["SELECT pg_try_advisory_lock(%s)"]
    assert database._operation_connection is None


def test_private_backup_publishes_only_a_complete_inode(tmp_path):
    tmp_path.chmod(0o700)
    final = tmp_path / "backup.dump"

    def fail(stream):
        stream.write(b"partial private fixture")
        raise RuntimeError("copy interrupted")

    with pytest.raises(RuntimeError):
        write_private_backup(final, fail)
    assert not final.exists()
    partials = list(tmp_path.glob("*.partial"))
    assert len(partials) == 1 and partials[0].stat().st_mode & 0o077 == 0

    def complete(stream):
        stream.write(b"complete opaque byte fixture")
        assert not final.exists()

    write_private_backup(final, complete)
    assert final.read_bytes() == b"complete opaque byte fixture"
    assert final.stat().st_mode & 0o077 == 0 and final.stat().st_nlink == 1
    assert os.stat(final).st_uid == os.getuid()
