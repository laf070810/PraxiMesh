"""Owned PostgreSQL generations for a maintenance-window deployment.

No database, backup or role is automatically deleted. A frozen generation has
no runtime CONNECT privilege or active sessions. Restores are independently
hashed before a caller may migrate or expose the candidate generation.
"""

import functools
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

INSTANCE = re.compile(r"[a-z][a-z0-9]{0,19}")
GENERATION = re.compile(r"[a-f0-9]{12}")
CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
OWNER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
MAX_DATA_BYTES = 10 * 1024**3
MAX_OPERATION_SECONDS = 900
DB_OPTIONS = (
    "-c statement_timeout=60000 -c lock_timeout=5000 "
    "-c idle_in_transaction_session_timeout=15000 -c transaction_timeout=900000"
)
BASE_WEB_READ = (
    "core_knowledgenode",
    "core_problem",
    "core_problemversion",
    "core_workerhealth",
    "auth_group",
    "auth_permission",
    "auth_group_permissions",
    "django_content_type",
)
FEATURE_READ_TABLES = {
    ("projects", "0001_initial"): (
        "projects_project",
        "projects_projectrevision",
        "projects_projectstage",
    ),
    ("localization", "0001_initial"): (
        "localization_knowledgesnapshot",
        "localization_knowledgetranslation",
        "localization_problemtranslation",
    ),
}
WEB_READ = BASE_WEB_READ + tuple(table for group in FEATURE_READ_TABLES.values() for table in group)
BASE_WEB_WRITE = (
    "auth_user",
    "auth_user_groups",
    "auth_user_user_permissions",
    "core_accountprofile",
    "core_draft",
    "core_enrollment",
    "core_submission",
    "core_ratebucket",
    "core_queuelock",
    "django_session",
)
FEATURE_TABLES = {
    ("learning", "0001_initial"): (
        "learning_bookmark",
        "learning_note",
        "learning_tutorialreading",
    ),
    ("community", "0001_initial"): (
        "community_post",
        "community_comment",
        "community_report",
        "community_appeal",
        "community_moderationevent",
    ),
    ("practice_api", "0001_initial"): (
        "practice_api_accesstoken",
        "practice_api_submissionrequest",
    ),
    ("showcase", "0001_initial"): (
        "showcase_publicprofile",
        "showcase_sharedresult",
    ),
}
WEB_WRITE = BASE_WEB_WRITE + tuple(table for group in FEATURE_TABLES.values() for table in group)
WORKER_PRIVILEGES = {
    "core_submission": "SELECT, UPDATE",
    "core_problemversion": "SELECT",
    "core_workerhealth": "SELECT, INSERT, UPDATE",
}


class DeploymentDatabaseError(RuntimeError):
    """Safe operational error: no underlying server output or credentials."""


def schema_tables(connection, base, features):
    """Use the recorded schema, including the previous generation during upgrades."""
    installed = set(connection.execute("SELECT app, name FROM public.django_migrations").fetchall())
    present = {
        row[0]
        for row in connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p')"
        ).fetchall()
    }
    expected = list(base)
    for migration, tables in features.items():
        if migration in installed:
            expected.extend(tables)
        elif present.intersection(tables):
            raise DeploymentDatabaseError("Feature tables exist without their recorded migration")
    if not set(expected).issubset(present):
        raise DeploymentDatabaseError("Recorded migrations are missing required runtime tables")
    return tuple(expected)


def web_write_tables(connection):
    return schema_tables(connection, BASE_WEB_WRITE, FEATURE_TABLES)


def web_read_tables(connection):
    return schema_tables(connection, BASE_WEB_READ, FEATURE_READ_TABLES)


def safe_errors(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (psycopg.Error, OSError, subprocess.SubprocessError):
            raise DeploymentDatabaseError(
                f"Database operation {function.__name__} failed"
            ) from None

    return wrapped


def loopback_port(output):
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})\n?", output)
    if not match or not 1 <= int(match[1]) <= 65535:
        raise DeploymentDatabaseError("PostgreSQL must publish exactly one IPv4 loopback port")
    return int(match[1])


def docker_output(*arguments):
    result = subprocess.run(
        ["docker", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise DeploymentDatabaseError("Cannot inspect the configured PostgreSQL container")
    return result.stdout.decode("utf-8")


def write_private_backup(path, writer):
    """Publish a complete dump without overwriting an existing path.

    Interrupted output stays in an explicitly named private .partial file. A
    hard link publishes the fully fsynced inode with no overwrite race.
    """
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise DeploymentDatabaseError("Backup directory must not contain symlinks")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DeploymentDatabaseError("Backup directory must be private and owned")
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DeploymentDatabaseError("Refusing to overwrite an existing backup path")
        partial = f".{path.name}.{uuid.uuid4().hex}.partial"
        descriptor = os.open(
            partial,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as backup:
            writer(backup)
            backup.flush()
            os.fsync(backup.fileno())
        os.link(
            partial, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
        )
        os.fsync(directory)
        os.unlink(partial, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


class DeploymentDatabase:
    @safe_errors
    def __init__(self, config):
        required = {
            "instance",
            "site_id",
            "container",
            "owner_user",
            "owner_password",
            "web_password",
            "worker_password",
        }
        if not isinstance(config, dict) or set(config) != required:
            raise DeploymentDatabaseError("Invalid database configuration keys")
        if any(
            not isinstance(value, str) or not value or "\0" in value for value in config.values()
        ):
            raise DeploymentDatabaseError("Invalid database configuration value")
        if not INSTANCE.fullmatch(config["instance"]):
            raise DeploymentDatabaseError("Invalid deployment instance identifier")
        if not re.fullmatch(r"[a-f0-9]{32}", config["site_id"]):
            raise DeploymentDatabaseError("Invalid deployment site identifier")
        if not CONTAINER.fullmatch(config["container"]) or not OWNER.fullmatch(
            config["owner_user"]
        ):
            raise DeploymentDatabaseError("Invalid PostgreSQL container or owner identifier")
        self.config = dict(config)
        self.instance, self.container = config["instance"], config["container"]
        self.site_id = config["site_id"]
        self._operation_connection = None
        self._lock_key = int.from_bytes(
            hashlib.sha256(f"praximesh-deployment:{self.instance}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        self.roles = {role: f"px_{self.instance}_{role}" for role in ["web", "worker"]}
        if config["owner_user"] in self.roles.values():
            raise DeploymentDatabaseError("Database owner must be distinct from runtime roles")
        raw = docker_output(
            "inspect",
            "--type",
            "container",
            "--format",
            '{"name":{{json .Name}},"running":{{json .State.Running}}}',
            self.container,
        )
        try:
            details = json.loads(raw)
        except (ValueError, TypeError):
            raise DeploymentDatabaseError("Invalid PostgreSQL container inspection") from None
        if details != {"name": "/" + self.container, "running": True}:
            raise DeploymentDatabaseError("PostgreSQL container name or running state differs")
        self.host = "127.0.0.1"
        self.port = loopback_port(docker_output("port", self.container, "5432/tcp"))

    def name(self, generation):
        if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
            raise DeploymentDatabaseError("Generation must be 12 lowercase hexadecimal characters")
        return f"px_{self.instance}_{generation}"

    def _validate_name(self, name):
        if not isinstance(name, str) or not re.fullmatch(
            rf"px_{self.instance}_[a-f0-9]{{12}}", name
        ):
            raise DeploymentDatabaseError("Database is outside this deployment instance")

    def _marker(self, kind):
        return f"praximesh deployment v1 instance={self.instance} site={self.site_id} object={kind}"

    def _connect(self, name, *, autocommit=False):
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=name,
            user=self.config["owner_user"],
            password=self.config["owner_password"],
            connect_timeout=5,
            sslmode="disable",
            autocommit=autocommit,
            application_name=f"praximesh-deploy-{self.instance}",
            options=DB_OPTIONS,
            keepalives=1,
            keepalives_idle=5,
            keepalives_interval=5,
            keepalives_count=2,
            tcp_user_timeout=15000,
        )

    @contextmanager
    def operation_lock(self):
        if self._operation_connection is not None:
            raise DeploymentDatabaseError("Nested database deployment operation is forbidden")
        try:
            with self._connect("postgres", autocommit=True) as connection:
                if not connection.execute(
                    "SELECT pg_try_advisory_lock(%s)", (self._lock_key,)
                ).fetchone()[0]:
                    raise DeploymentDatabaseError("Another coordinator owns this database instance")
                self._operation_connection = connection
                try:
                    yield self
                finally:
                    # Closing the session also releases its advisory lock on
                    # exceptions; no explicit unlock can hide the original error.
                    self._operation_connection = None
        except (psycopg.Error, OSError):
            raise DeploymentDatabaseError("Database operation lock failed") from None

    def _require_operation_lock(self):
        connection = self._operation_connection
        if connection is None or connection.closed:
            raise DeploymentDatabaseError("Database changes require operation_lock")
        unsigned = self._lock_key & ((1 << 64) - 1)
        if not connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
            "AND pid=pg_backend_pid() AND classid=%s::oid AND objid=%s::oid "
            "AND objsubid=1 AND granted)",
            (unsigned >> 32, unsigned & 0xFFFFFFFF),
        ).fetchone()[0]:
            raise DeploymentDatabaseError("Database deployment lock was lost")

    def _check_role(self, row, role):
        # comment, login, superuser, create-db, create-role, replication,
        # bypass-RLS, inherit, memberships
        if row != (self._marker(role), True, False, False, False, False, False, False, False):
            raise DeploymentDatabaseError(
                "Existing runtime role has no matching ownership or has excess privileges"
            )

    def _require_roles(self):
        with self._connect("postgres") as connection:
            for role, name in self.roles.items():
                row = connection.execute(
                    "SELECT shobj_description(oid, 'pg_authid'), rolcanlogin, rolsuper, "
                    "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, "
                    "EXISTS (SELECT 1 FROM pg_auth_members m WHERE member = r.oid "
                    "OR (roleid=r.oid AND member<>(SELECT oid FROM pg_roles WHERE rolname=%s))) "
                    "FROM pg_roles r WHERE rolname = %s",
                    (self.config["owner_user"], name),
                ).fetchone()
                self._check_role(row, role)

    @safe_errors
    def ensure_roles(self):
        self._require_operation_lock()
        with self._connect("postgres") as connection:
            for role, name in self.roles.items():
                row = connection.execute(
                    "SELECT shobj_description(oid, 'pg_authid'), rolcanlogin, rolsuper, "
                    "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, "
                    "EXISTS (SELECT 1 FROM pg_auth_members m WHERE member = r.oid "
                    "OR (roleid=r.oid AND member<>(SELECT oid FROM pg_roles WHERE rolname=%s))) "
                    "FROM pg_roles r WHERE rolname = %s",
                    (self.config["owner_user"], name),
                ).fetchone()
                if row is None:
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                            "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}"
                        ).format(sql.Identifier(name), sql.Literal(self.config[role + "_password"]))
                    )
                    connection.execute(
                        sql.SQL("COMMENT ON ROLE {} IS {}").format(
                            sql.Identifier(name), sql.Literal(self._marker(role))
                        )
                    )
                else:
                    self._check_role(row, role)
                    connection.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(name), sql.Literal(self.config[role + "_password"])
                        )
                    )

    @safe_errors
    def require_owned(self, name):
        self._validate_name(name)
        with self._connect("postgres") as connection:
            row = connection.execute(
                "SELECT pg_get_userbyid(datdba), shobj_description(oid, 'pg_database') "
                "FROM pg_database WHERE datname = %s",
                (name,),
            ).fetchone()
        if row != (self.config["owner_user"], self._marker("database")):
            raise DeploymentDatabaseError("Database is missing or has no matching owner and marker")

    @safe_errors
    def connection(self, name):
        self.require_owned(name)
        return self._connect(name)

    @safe_errors
    def create(self, name):
        self._require_operation_lock()
        self._validate_name(name)
        with self._connect("postgres", autocommit=True) as connection:
            if connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone():
                raise DeploymentDatabaseError("Refusing to reuse an existing database")
            # A crash before marking leaves a closed, unowned database; recovery
            # must not silently adopt it. CREATE DATABASE cannot be transactional.
            connection.execute(
                sql.SQL(
                    "CREATE DATABASE {} OWNER {} TEMPLATE template0 ALLOW_CONNECTIONS false"
                ).format(sql.Identifier(name), sql.Identifier(self.config["owner_user"]))
            )
            with connection.transaction():
                connection.execute(
                    sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                        sql.Identifier(name), sql.Literal(self._marker("database"))
                    )
                )
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
                )
                connection.execute(
                    sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(name))
                )

    @safe_errors
    def grant(self, name):
        self._require_operation_lock()
        self._require_frozen(name)
        self._require_roles()
        with self._connect(name) as connection:
            writable = web_write_tables(connection)
            readable = web_read_tables(connection)
            # No implicit grants to newly migrated tables. Every writable table
            # and every sequence is derived from this reviewed allowlist.
            principals = sql.SQL("PUBLIC, {}, {}").format(*map(sql.Identifier, self.roles.values()))
            for objects in [
                "SCHEMA public",
                "ALL TABLES IN SCHEMA public",
                "ALL SEQUENCES IN SCHEMA public",
                "ALL FUNCTIONS IN SCHEMA public",
            ]:
                connection.execute(
                    sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(objects), principals)
                )
            for role in self.roles.values():
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
                )
            for tables, privileges in [
                (readable, "SELECT"),
                (writable, "SELECT, INSERT, UPDATE, DELETE"),
            ]:
                connection.execute(
                    sql.SQL("GRANT {} ON {} TO {}").format(
                        sql.SQL(privileges),
                        sql.SQL(", ").join(sql.Identifier("public", table) for table in tables),
                        sql.Identifier(self.roles["web"]),
                    )
                )
            for table, privileges in WORKER_PRIVILEGES.items():
                connection.execute(
                    sql.SQL("GRANT {} ON {} TO {}").format(
                        sql.SQL(privileges),
                        sql.Identifier("public", table),
                        sql.Identifier(self.roles["worker"]),
                    )
                )
            for schema, sequence in self._web_sequences(connection):
                connection.execute(
                    sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                        sql.Identifier(schema, sequence), sql.Identifier(self.roles["web"])
                    )
                )
        self.thaw(name)

    def _web_sequences(self, connection):
        return set(
            connection.execute(
                "SELECT ns.nspname, s.relname FROM pg_class s JOIN pg_namespace ns ON ns.oid=s.relnamespace "
                "JOIN pg_depend d ON d.objid=s.oid AND d.classid='pg_class'::regclass "
                "JOIN pg_class t ON t.oid=d.refobjid JOIN pg_namespace tn ON tn.oid=t.relnamespace "
                "WHERE s.relkind='S' AND d.deptype IN ('a','i') AND tn.nspname='public' AND t.relname=ANY(%s)",
                (list(WEB_WRITE),),
            ).fetchall()
        )

    def _verify_permissions(self, name, *, connected):
        """Check effective privileges, including column grants and grant options."""
        with self.connection(name) as connection:
            writable = web_write_tables(connection)
            readable = web_read_tables(connection)
            sequences = self._web_sequences(connection)
            if connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database d, "
                "LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a "
                "WHERE d.datname=%s AND a.grantee=0)",
                (name,),
            ).fetchone()[0]:
                raise DeploymentDatabaseError("Database PUBLIC privileges must remain revoked")
            for role, identity in self.roles.items():
                expected = (
                    {
                        **{table: {"SELECT"} for table in readable},
                        **{table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table in writable},
                    }
                    if role == "web"
                    else {
                        table: set(privileges.split(", "))
                        for table, privileges in WORKER_PRIVILEGES.items()
                    }
                )
                privileges = [
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                    "MAINTAIN",
                ]
                rows = connection.execute(
                    "SELECT c.relname,p.priv,has_table_privilege(%s,c.oid,p.priv), "
                    "has_table_privilege(%s,c.oid,p.priv || ' WITH GRANT OPTION') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "CROSS JOIN unnest(%s::text[]) p(priv) WHERE n.nspname='public' "
                    "AND c.relkind IN ('r','p','v','m','f')",
                    (identity, identity, privileges),
                ).fetchall()
                if not expected.keys() <= {row[0] for row in rows} or any(
                    allowed != (privilege in expected.get(table, set())) or grantable
                    for table, privilege, allowed, grantable in rows
                ):
                    raise DeploymentDatabaseError(
                        "Runtime table privileges differ from the reviewed policy"
                    )
                columns = connection.execute(
                    "SELECT c.relname,p.priv,has_any_column_privilege(%s,c.oid,p.priv), "
                    "has_any_column_privilege(%s,c.oid,p.priv || ' WITH GRANT OPTION') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "CROSS JOIN unnest(%s::text[]) p(priv) WHERE n.nspname='public' "
                    "AND c.relkind IN ('r','p','v','m','f')",
                    (identity, identity, ["SELECT", "INSERT", "UPDATE", "REFERENCES"]),
                ).fetchall()
                if any(
                    allowed != (privilege in expected.get(table, set())) or grantable
                    for table, privilege, allowed, grantable in columns
                ):
                    raise DeploymentDatabaseError("Unexpected runtime column privileges")
                sequence_rows = connection.execute(
                    "SELECT n.nspname,c.relname,p.priv,has_sequence_privilege(%s,c.oid,p.priv), "
                    "has_sequence_privilege(%s,c.oid,p.priv || ' WITH GRANT OPTION') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "CROSS JOIN unnest(%s::text[]) p(priv) WHERE n.nspname='public' AND c.relkind='S'",
                    (identity, identity, ["SELECT", "USAGE", "UPDATE"]),
                ).fetchall()
                if any(
                    allowed
                    != (
                        role == "web"
                        and (schema, sequence) in sequences
                        and privilege in {"SELECT", "USAGE"}
                    )
                    or grantable
                    for schema, sequence, privilege, allowed, grantable in sequence_rows
                ):
                    raise DeploymentDatabaseError("Unexpected runtime sequence privileges")
                permissions = connection.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT'), "
                    "has_database_privilege(%s,%s,'CREATE'), has_database_privilege(%s,%s,'TEMP'), "
                    "has_schema_privilege(%s,'public','USAGE'), has_schema_privilege(%s,'public','CREATE'), "
                    "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='public' AND has_function_privilege(%s,p.oid,'EXECUTE'))",
                    (identity, name, identity, name, identity, name, identity, identity, identity),
                ).fetchone()
                if permissions != (connected, False, False, True, False, False):
                    raise DeploymentDatabaseError(
                        "Unexpected runtime database, schema or function privileges"
                    )

    def _sessions(self, connection, name):
        return connection.execute(
            "SELECT pid, usename FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid() "
            "AND backend_type='client backend'",
            (name,),
        ).fetchall()

    @safe_errors
    def freeze(self, name):
        self._require_operation_lock()
        self.require_owned(name)
        self._require_roles()
        with self._connect("postgres", autocommit=True) as connection:
            if any(role not in self.roles.values() for _, role in self._sessions(connection, name)):
                raise DeploymentDatabaseError("Unexpected database sessions prevent freezing")
            with connection.transaction():
                connection.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, {}, {}").format(
                        sql.Identifier(name), *map(sql.Identifier, self.roles.values())
                    )
                )
            for pid, role in self._sessions(connection, name):
                if role not in self.roles.values():
                    raise DeploymentDatabaseError(
                        "Unexpected database session appeared while freezing"
                    )
                connection.execute(
                    "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
                    "WHERE pid=%s AND datname=%s AND usename=ANY(%s)",
                    (pid, name, list(self.roles.values())),
                )
        self._require_frozen(name)

    def _require_frozen(self, name):
        self.require_owned(name)
        with self._connect("postgres") as connection:
            if self._sessions(connection, name) or any(
                connection.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT')", (role, name)
                ).fetchone()[0]
                for role in self.roles.values()
            ):
                raise DeploymentDatabaseError("Database must remain frozen without active sessions")
            if connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_prepared_xacts WHERE database=%s)", (name,)
            ).fetchone()[0]:
                raise DeploymentDatabaseError("Prepared transactions prevent database freezing")

    @safe_errors
    def thaw(self, name):
        self._require_operation_lock()
        self.require_owned(name)
        self._require_roles()
        # Verify effective access before reopening either login. A failed check
        # leaves CONNECT closed, including unexpected PUBLIC/column grants.
        with self._connect("postgres") as connection:
            if any(role not in self.roles.values() for _, role in self._sessions(connection, name)):
                raise DeploymentDatabaseError(
                    "Unexpected database sessions prevent reopening runtime access"
                )
            connected = [
                connection.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT')", (role, name)
                ).fetchone()[0]
                for role in self.roles.values()
            ]
        if connected not in [[False, False], [True, True]]:
            raise DeploymentDatabaseError("Runtime CONNECT grants are inconsistent")
        self._verify_permissions(name, connected=connected[0])
        with self._connect("postgres") as connection:
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(name), *map(sql.Identifier, self.roles.values())
                )
            )

    def _pg_command(self, command, name, *, stdin=None, stdout=None):
        self._require_operation_lock()
        self.require_owned(name)
        if command not in {"pg_dump", "pg_restore"}:
            raise DeploymentDatabaseError("Unsupported database copy command")
        application = f"px-copy-{self.site_id[:16]}-{uuid.uuid4().hex[:16]}"
        environment = dict(
            os.environ,
            PGPASSWORD=self.config["owner_password"],
            PGUSER=self.config["owner_user"],
            PGHOST="127.0.0.1",
            PGPORT="5432",
            PGSSLMODE="disable",
            PGCONNECT_TIMEOUT="5",
            PGAPPNAME=application,
            PGOPTIONS=DB_OPTIONS,
        )
        arguments = ["docker", "exec", "--interactive"]
        for key in [
            "PGPASSWORD",
            "PGUSER",
            "PGHOST",
            "PGPORT",
            "PGSSLMODE",
            "PGCONNECT_TIMEOUT",
            "PGAPPNAME",
            "PGOPTIONS",
        ]:
            arguments.extend(["--env", key])
        # This independent container timer survives coordinator/client death.
        # pg_dump/pg_restore deliberately disable all PostgreSQL GUC timeouts.
        arguments.extend(
            [
                self.container,
                "timeout",
                "-s",
                "TERM",
                "-k",
                "5",
                str(MAX_OPERATION_SECONDS),
                command,
                "--dbname=" + name,
            ]
        )
        arguments.extend(
            ["--format=custom"]
            if command == "pg_dump"
            else ["--exit-on-error", "--single-transaction", "--no-owner", "--no-acl"]
        )
        try:
            result = subprocess.run(
                arguments,
                env=environment,
                stdin=stdin or subprocess.DEVNULL,
                stdout=stdout or subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=MAX_OPERATION_SECONDS,
                check=False,
            )
            if result.returncode:
                raise DeploymentDatabaseError(
                    f"Database {command} failed; existing data and backup retained"
                )
        except BaseException:
            # Killing Docker CLI does not prove pg_restore stopped. Terminate
            # only this command's randomly identified owner session; even a
            # killed coordinator still has the separate container timer.
            self._terminate_copy(name, application)
            raise

    def _terminate_copy(self, name, application):
        with self._connect("postgres", autocommit=True) as connection:
            terminated = connection.execute(
                "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
                "WHERE datname=%s AND usename=%s AND application_name=%s "
                "AND pid<>pg_backend_pid()",
                (name, self.config["owner_user"], application),
            ).fetchall()
            if any(not row[0] for row in terminated):
                raise DeploymentDatabaseError("Database copy session cleanup did not complete")

    def _signature(self, name):
        tables, sequences, total = {}, {}, 0
        deadline = time.monotonic() + MAX_OPERATION_SECONDS
        with self.connection(name) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN "
                "('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_toast%' "
                "AND nspname NOT LIKE 'pg_temp%') OR EXISTS (SELECT 1 FROM pg_largeobject_metadata)"
            ).fetchone()[0]:
                raise DeploymentDatabaseError(
                    "Unsupported schema or large objects in deployment database"
                )
            relations = connection.execute(
                "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind IN ('r','S','m','p','v','f') ORDER BY c.relname"
            ).fetchall()
            if len(relations) > 256:
                raise DeploymentDatabaseError(
                    "Too many database relations for bounded verification"
                )
            for table, kind in relations:
                if time.monotonic() >= deadline:
                    raise DeploymentDatabaseError("Database verification time limit exceeded")
                if kind == "S":
                    state = connection.execute(
                        sql.SQL("SELECT last_value, is_called FROM {}").format(
                            sql.Identifier("public", table)
                        )
                    ).fetchone()
                    definition = connection.execute(
                        "SELECT seqstart,seqincrement,seqmax,seqmin,seqcache,seqcycle "
                        "FROM pg_sequence WHERE seqrelid=%s::regclass",
                        ("public." + table,),
                    ).fetchone()
                    sequences[table] = {"state": state, "definition": definition}
                    continue
                if kind != "r":
                    raise DeploymentDatabaseError(
                        "Unsupported relation type in deployment database"
                    )
                columns = connection.execute(
                    "SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull "
                    "FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped "
                    "ORDER BY a.attnum",
                    ("public." + table,),
                ).fetchall()
                keys = connection.execute(
                    "SELECT a.attname FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
                    "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY k.ord",
                    ("public." + table,),
                ).fetchall()
                if not keys:
                    raise DeploymentDatabaseError(
                        "Restoration verification requires primary keys on all tables"
                    )
                rows = connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
                ).fetchone()[0]
                digest, size = hashlib.sha256(), 0
                with connection.cursor().copy(
                    sql.SQL("COPY (SELECT * FROM {} ORDER BY {}) TO STDOUT (FORMAT BINARY)").format(
                        sql.Identifier("public", table),
                        sql.SQL(",").join(sql.Identifier(key[0]) for key in keys),
                    )
                ) as stream:
                    for block in stream:
                        size += len(block)
                        total += len(block)
                        if total > MAX_DATA_BYTES or time.monotonic() >= deadline:
                            raise DeploymentDatabaseError(
                                "Database verification exceeded its bounded data limit"
                            )
                        digest.update(block)
                tables[table] = {
                    "rows": rows,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "columns": columns,
                    "primary_key": keys,
                }
        return {"tables": tables, "sequences": sequences}

    @safe_errors
    def backup_restore(self, source, target, path):
        self._require_operation_lock()
        self._require_frozen(source)
        self._validate_name(target)
        if source == target:
            raise DeploymentDatabaseError("Restore target must be a distinct generation")
        path = Path(path).absolute()
        source_signature = self._signature(source)
        write_private_backup(
            path, lambda backup: self._pg_command("pg_dump", source, stdout=backup)
        )
        self._require_frozen(source)
        if self._signature(source) != source_signature:
            raise DeploymentDatabaseError("Frozen source changed during backup")
        self.create(target)
        with path.open("rb") as backup:
            digest = hashlib.file_digest(backup, "sha256").hexdigest()
            backup.seek(0)
            self._pg_command("pg_restore", target, stdin=backup)
        self._require_frozen(target)
        if self._signature(target) != source_signature:
            raise DeploymentDatabaseError("Restored database rows, schema or sequence state differ")
        encoded = json.dumps(source_signature, sort_keys=True, separators=(",", ":")).encode()
        return {
            "backup_sha256": digest,
            "backup_bytes": path.stat().st_size,
            "data_sha256": hashlib.sha256(encoded).hexdigest(),
            "tables": len(source_signature["tables"]),
            "rows": sum(table["rows"] for table in source_signature["tables"].values()),
            "sequences": len(source_signature["sequences"]),
        }

    @safe_errors
    def invalidate_health(self, name):
        self._require_operation_lock()
        with self.connection(name) as connection:
            connection.execute("UPDATE core_workerhealth SET ready=false")

    def url(self, name, role):
        self._validate_name(name)
        if role not in self.roles and role != "owner":
            raise DeploymentDatabaseError("Invalid database connection role")
        user = self.config["owner_user"] if role == "owner" else self.roles[role]
        password = self.config[role + "_password"]
        return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{self.container}:5432/{name}?sslmode=disable"
