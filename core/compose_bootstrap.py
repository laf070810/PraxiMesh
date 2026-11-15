"""Initialize one prepared, fresh PostgreSQL installation without Docker or GPU access."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from psycopg import sql

from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError

ROOT = Path(__file__).resolve().parents[1]
MAX_SPEC = 32768


def validate_spec(value):
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "instance",
            "site_id",
            "generation",
            "source_id",
            "release_sha256",
            "initial_content",
            "database",
            "application_secret",
            "wasi_sha256",
        }
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or type(value["initial_content"]) is not bool
        or type(value["application_secret"]) is not str
        or len(value["application_secret"]) < 50
        or value["application_secret"].startswith("REPLACE")
    ):
        raise ValueError("Invalid prepared Compose bootstrap specification")
    for name, length in (
        ("site_id", 32),
        ("generation", 12),
        ("source_id", 64),
        ("release_sha256", 64),
        ("wasi_sha256", 64),
    ):
        if type(value[name]) is not str or not re.fullmatch(rf"[a-f0-9]{{{length}}}", value[name]):
            raise ValueError("Invalid frozen Compose installation identity")
    database = DeploymentDatabase.for_compose_bootstrap(value["database"])
    if database.instance != value["instance"] or database.site_id != value["site_id"]:
        raise ValueError("Bootstrap database belongs to another installation")
    database.name(value["generation"])
    return value


def configuration_digest(spec):
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Bootstrap:
    def __init__(self, spec, database=None):
        self.spec = validate_spec(spec)
        self.database = database or DeploymentDatabase.for_compose_bootstrap(spec["database"])
        if self.database.config != spec["database"]:
            raise ValueError("Bootstrap connection and original configuration differ")
        self.name = self.database.name(spec["generation"])
        self.schema = "px_bootstrap_" + spec["instance"]
        self.digest = configuration_digest(spec)
        self.marker = "praximesh compose bootstrap v1 site=" + spec["site_id"]

    def journal(self):
        """Create the private identity and initial intent in one transaction."""
        self.database._require_operation_lock()
        with self.database._connect("postgres") as connection:
            row = connection.execute(
                "SELECT pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') "
                "FROM pg_namespace WHERE nspname=%s",
                (self.schema,),
            ).fetchone()
            if row is None:
                # A missing installation journal cannot authorize an existing DB.
                if self.database.owned_exists(self.name):
                    raise DeploymentDatabaseError("Existing database has no bootstrap journal")
                connection.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        sql.Identifier(self.schema),
                        sql.Identifier(self.database.config["owner_user"]),
                    )
                )
                connection.execute(
                    sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                        sql.Identifier(self.schema), sql.Literal(self.marker)
                    )
                )
                connection.execute(
                    sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                        sql.Identifier(self.schema)
                    )
                )
                connection.execute(
                    sql.SQL(
                        "CREATE TABLE {}.installation (id integer PRIMARY KEY CHECK (id=1), "
                        "configuration text NOT NULL, database_name text NOT NULL, "
                        "phase text NOT NULL, completed_steps integer NOT NULL)"
                    ).format(sql.Identifier(self.schema))
                )
                connection.execute(
                    sql.SQL("INSERT INTO {}.installation VALUES (1,%s,%s,'planned',0)").format(
                        sql.Identifier(self.schema)
                    ),
                    (self.digest, self.name),
                )
            elif row != (self.database.config["owner_user"], self.marker):
                raise DeploymentDatabaseError(
                    "Bootstrap metadata has a different owner or identity"
                )
            rows = connection.execute(
                sql.SQL(
                    "SELECT configuration,database_name,phase,completed_steps FROM {}.installation"
                ).format(sql.Identifier(self.schema))
            ).fetchall()
            if (
                len(rows) != 1
                or rows[0][:2] != (self.digest, self.name)
                or rows[0][2] not in {"planned", "initializing", "granting", "ready"}
                or not 0 <= rows[0][3] <= len(self.commands())
                or rows[0][2] == "planned"
                and rows[0][3] != 0
                or rows[0][2] in {"granting", "ready"}
                and rows[0][3] != len(self.commands())
            ):
                raise DeploymentDatabaseError("Bootstrap configuration or original phase changed")
            return rows[0][2:]

    def save(self, phase, completed):
        self.database._require_operation_lock()
        with self.database._connect("postgres") as connection:
            cursor = connection.execute(
                sql.SQL(
                    "UPDATE {}.installation SET phase=%s,completed_steps=%s "
                    "WHERE id=1 AND configuration=%s AND database_name=%s"
                ).format(sql.Identifier(self.schema)),
                (phase, completed, self.digest, self.name),
            )
            if cursor.rowcount != 1:
                raise DeploymentDatabaseError("Original bootstrap journal changed")

    def commands(self):
        commands = [("migrate", "--noinput")]
        if self.spec["initial_content"]:
            commands.extend(
                [
                    ("content_import", "--translations", str(ROOT / "content/en.json")),
                    ("project_import", str(ROOT / "content/projects.json")),
                    ("challenge_import", str(ROOT / "content/challenges.json")),
                ]
            )
        return commands

    def manage(self, arguments):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PRAXIMESH_MODE": "local",
            "APP_SECRET_KEY": self.spec["application_secret"],
            "DATABASE_URL": self.database.url(self.name, "owner"),
            "DB_SSLMODE": "disable",
            "JUDGE_ENABLED": "0",
            "JUDGE_GPU_DEVICES": "",
            "JUDGE_WASI_SHA256": self.spec["wasi_sha256"],
            "COACHING_ENABLED": "0",
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "manage.py"), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=300,
        )
        if result.returncode:
            # Django/connection diagnostics may contain private configuration.
            raise DeploymentDatabaseError("Frozen application initialization command failed")

    def finish_grants(self):
        """Resume a lost grant acknowledgement without freezing a live installation."""
        self.database.require_owned(self.name)
        self.database._require_roles()
        with self.database._connect("postgres") as connection:
            connected = [
                connection.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT')", (role, self.name)
                ).fetchone()[0]
                for role in self.database.roles.values()
            ]
        if connected == [False, False]:
            self.database.grant(self.name)
        elif connected != [True, True]:
            raise DeploymentDatabaseError("Runtime connection permissions changed during bootstrap")
        self.database._verify_permissions(self.name, connected=True)

    def run(self):
        database = self.database
        with database.operation_lock():
            phase, completed = self.journal()
            if phase == "ready":
                database.require_owned(self.name)
                database._require_roles()
                self.manage(("migrate", "--check", "--noinput"))
                database._verify_permissions(self.name, connected=True)
                return {"initialized": False, "database": self.name}
            if phase == "planned":
                database.ensure_roles()
                if not database.owned_exists(self.name):
                    database.create(self.name)
                database._require_frozen(self.name)
                self.save("initializing", completed)
                phase = "initializing"
            else:
                database.require_owned(self.name)
                database._require_roles()
            if phase == "initializing":
                database._require_frozen(self.name)
                for index, command in enumerate(self.commands()):
                    if index >= completed:
                        self.manage(command)
                        completed = index + 1
                        self.save("initializing", completed)
                self.save("granting", completed)
            self.finish_grants()
            self.save("ready", completed)
            return {"initialized": True, "database": self.name}


def main():
    try:
        encoded = os.environ.get("PRAXIMESH_COMPOSE_BOOTSTRAP", "")
        if not 1 <= len(encoded.encode()) <= MAX_SPEC:
            raise ValueError("Prepared bootstrap settings are required")
        spec = validate_spec(json.loads(encoded))
        print(json.dumps(Bootstrap(spec).run()))
        return 0
    except Exception as error:
        print("Compose database initialization failed: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
