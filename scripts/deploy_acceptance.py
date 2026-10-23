"""Rehearse journalled deployment with real HTTPS, PostgreSQL and WASI judging.

All resources are disposable, owner-labelled and loopback/private. The default
mode applies the same release to successive generations. --upgrade-only instead
installs an explicitly verified previous release, creates real private data and
formal AC, then applies the next schema without republishing the catalogue.
The B2-to-C rehearsal separately performs an explicit trusted-owner publication
after verifying preservation, then checks a real new CPU reservation submission.
Interruption checkpoints only pause a real CLI process with SIGSTOP. They do not
alter its journal, database, verdicts or production implementation.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.deploy import Deployment  # noqa: E402
from scripts.deploy_proxy import DeploymentProxy  # noqa: E402
from scripts.production_acceptance import POSTGRES, Client, pki, port  # noqa: E402
from scripts.release import inspect_images, verify  # noqa: E402

CORRECT = "def solve(values):\n    return sum(x for x in values if x > 0)\n"
LABEL = "org.praximesh.acceptance"
UPGRADE_MIGRATIONS = {("learning", "0001_initial"), ("community", "0001_initial")}
UPGRADE_TABLES = (
    "learning_bookmark",
    "learning_note",
    "learning_tutorialreading",
    "community_post",
    "community_comment",
    "community_report",
    "community_appeal",
    "community_moderationevent",
)
B2_MIGRATIONS = {
    ("practice_api", "0001_initial"),
    ("showcase", "0001_initial"),
    ("projects", "0001_initial"),
    ("localization", "0001_initial"),
    ("localization", "0002_backfill_snapshots"),
}
C_MIGRATIONS = {
    ("core", "0003_cpu_reservations"),
    ("community", "0002_post_blog_kind"),
    ("organizations", "0001_initial"),
    ("organizations", "0002_publication_lock"),
    ("coaching", "0001_initial"),
    ("coaching", "0002_slot_lock"),
}
D_MIGRATIONS = {("challenges", "0001_initial")}
F_MIGRATIONS = {("community", "0003_problem_feedback")}

# This trusted administrative program runs only in the labelled worker container.
# Killing its exact controller models unexpected process death. `docker kill`
# would instead mark the container manually stopped and suppress unless-stopped.
KILL_WORKER = """
import os, signal, socket, sys
from pathlib import Path
name = sys.argv[1]
assert socket.gethostname() == name
expected = [b'manage.py', b'wasi_service', b'--worker-id', name.encode()]
matches = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        arguments = path.read_bytes().rstrip(b'\\0').split(b'\\0')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    if arguments[1:] == expected and arguments[0].rsplit(b'/', 1)[-1] in (b'python', b'python3', b'python3.11'):
        matches.append(int(path.parent.name))
assert len(matches) == 1 and matches[0] > 1
print('OWNED_WORKER_SIGKILL', flush=True)
os.kill(matches[0], signal.SIGKILL)
"""

# The real coordinator persists its own journal and performs every transition.
# Only the execution schedule changes. The parent sends SIGKILL after observing
# this independent checkpoint AND the live stopped process state.
INTERRUPT = """
import json, os, signal
from pathlib import Path
from scripts.deploy import Deployment, main
checkpoint = Path(os.environ['PRAXIMESH_ACCEPTANCE_CHECKPOINT'])
def pause(deployment):
    temporary = checkpoint.with_suffix('.pending')
    with temporary.open('x') as stream:
        json.dump({'pid': os.getpid(), 'phase': deployment.state['operation']['phase']}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, checkpoint)
    os.kill(os.getpid(), signal.SIGSTOP)
if os.environ['PRAXIMESH_ACCEPTANCE_INTERRUPT'] == 'before_validation':
    original = Deployment.check
    def check(self, generation, token):
        pause(self)
        return original(self, generation, token)
    Deployment.check = check
else:
    original = Deployment.health
    def health(self, generation, *, external):
        if external:
            pause(self)
        return original(self, generation, external=external)
    Deployment.health = health
main()
"""


def command(*arguments, environment=None, timeout=120):
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=True,
        timeout=timeout,
    ).stdout


def docker(*arguments, environment=None, timeout=120):
    return command("docker", *arguments, environment=environment, timeout=timeout)


def stop(process):
    if process.poll() is None:
        # Includes SIGSTOP checkpoints; SIGTERM alone cannot finish those.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


class Rehearsal:
    def __init__(self, options, directory):
        self.options, self.directory = options, directory
        self.token = uuid.uuid4().hex[:12]
        self.instance = "a" + self.token
        self.prefix = "praximesh-deploy-" + self.token
        self.names = {
            role: self.prefix + "-" + role for role in ["pg", "backend", "frontend", "data"]
        }
        self.state_dir = directory / "state"
        self.config_path = directory / "deployment-config.json"
        self.site_id = None
        self.processes = []
        self.log_streams = []
        self.accounts = {}
        self.config = None
        self.result = {
            "passed": False,
            "release_sha256": options.manifest_sha256,
            "scope": "synthetic data; private Docker networks; loopback HTTPS; same release for successful generations",
            "schema_changing_upgrade_tested": False,
            "schema_failure_rollback_tested": False,
            "worker_restart_only": options.worker_restart_only,
            "upgrade_only": options.upgrade_only,
            "worker_auto_restart_recovery_tested": False,
            "public_CA_or_system_trust_tested": False,
        }
        if options.upgrade_only:
            self.result.update(
                scope="Phase A to B1 schema upgrade; synthetic private data; original eight-problem catalogue retained; private Docker networks; loopback HTTPS",
                previous_release_sha256=options.previous_manifest_sha256,
                feature_role_permissions_tested=False,
                expanded_catalogue_published=False,
            )

    def labelled(self, resource, label):
        return docker(resource, "ls", "--quiet", "--filter", "label=" + label).decode().splitlines()

    def start(self):
        label = LABEL + "=" + self.token
        docker("network", "create", "--label", label, "--internal", self.names["backend"])
        docker("network", "create", "--label", label, self.names["frontend"])
        docker("volume", "create", "--label", label, self.names["data"])
        credentials = {role: secrets.token_urlsafe(36) for role in ["owner", "web", "worker"]}
        docker(
            "run",
            "--detach",
            "--name",
            self.names["pg"],
            "--label",
            label,
            "--network",
            self.names["frontend"],
            "--memory=512m",
            "--pids-limit=128",
            "--mount",
            f"type=volume,src={self.names['data']},dst=/var/lib/postgresql/data",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_PASSWORD",
            POSTGRES,
            environment=dict(os.environ, POSTGRES_PASSWORD=credentials["owner"]),
        )
        docker("network", "connect", self.names["backend"], self.names["pg"])
        self.db_port = int(
            docker("port", self.names["pg"], "5432/tcp").decode().strip().rsplit(":", 1)[1]
        )
        deadline = time.monotonic() + 45
        while True:
            try:
                with psycopg.connect(
                    host="127.0.0.1",
                    port=self.db_port,
                    dbname="postgres",
                    user="postgres",
                    password=credentials["owner"],
                    connect_timeout=1,
                ):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        cert, key, self.pin, self.context = pki(self.directory)
        self.base = f"https://127.0.0.1:{port()}"
        self.config = {
            "format": 1,
            "instance": self.instance,
            "database": {
                "container": self.names["pg"],
                "owner_user": "postgres",
                "owner_password": credentials["owner"],
                "web_password": credentials["web"],
                "worker_password": credentials["worker"],
            },
            "networks": {name: self.names[name] for name in ["backend", "frontend"]},
            "application": {
                "secret_key": secrets.token_urlsafe(64),
                "worker_secret": secrets.token_urlsafe(64),
                "registration_open": True,
            },
            "proxy": {
                "caddy": str(self.options.caddy),
                "origin": self.base,
                "bind": "127.0.0.1",
                "tls_cert": str(cert),
                "tls_key": str(key),
            },
            "health_ca": str(self.directory / "ca.pem"),
        }
        descriptor = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.config, stream)
        self.run("init", log="init")
        self.site_id = self.journal()["site_id"]
        self.proxy = DeploymentProxy(self.config["proxy"], self.state_dir)
        self.proxy_process = self.spawn("serve-proxy", log="proxy")
        self.wait_proxy("maintenance")
        assert Client(self.base, self.context).request("/")[0] == 503

    def wait_proxy(self, mode):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proxy_process.poll() is not None:
                raise RuntimeError("Dedicated Caddy terminated before readiness")
            try:
                if self.proxy.probe()["mode"] == mode:
                    return
            except (RuntimeError, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError("Dedicated Caddy did not enter the required mode")

    def cli(self, *arguments, interrupted=False):
        prefix = (
            [sys.executable, "-c", INTERRUPT]
            if interrupted
            else [sys.executable, str(ROOT / "scripts/deploy.py")]
        )
        return [
            *prefix,
            "--config",
            str(self.config_path),
            "--state",
            str(self.state_dir),
            *arguments,
        ]

    def spawn(self, *arguments, log, interrupt=None):
        stream = (self.options.artifacts / (log + ".log")).open("wb")
        self.log_streams.append(stream)
        environment = dict(os.environ, PYTHONUNBUFFERED="1")
        if interrupt:
            environment.update(
                PRAXIMESH_ACCEPTANCE_INTERRUPT=interrupt,
                PRAXIMESH_ACCEPTANCE_CHECKPOINT=str(self.directory / (log + ".checkpoint")),
            )
        process = subprocess.Popen(
            self.cli(*arguments, interrupted=bool(interrupt)),
            cwd=ROOT,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def run(self, *arguments, log):
        process = self.spawn(*arguments, log=log)
        result = process.wait(timeout=900)
        if result:
            raise RuntimeError(f"Deployment {log} failed; see its bounded rehearsal log")

    def journal(self):
        # Read only. No acceptance test ever creates/edits this authoritative file.
        return json.loads((self.state_dir / "deployment.json").read_text())

    def apply_args(self, *, initial=False, previous=False):
        if previous and not (initial and self.options.upgrade_only):
            raise ValueError(
                "The previous release is only an upgrade rehearsal's initial generation"
            )
        if self.options.upgrade_only and initial and not previous:
            raise ValueError("An upgrade must preserve the previous catalogue")
        return [
            "apply",
            "--release",
            str(self.options.previous_release if previous else self.options.release),
            "--sha256",
            self.options.previous_manifest_sha256 if previous else self.options.manifest_sha256,
            *(["--initial-content"] if initial else []),
        ]

    def connection(self, generation):
        return psycopg.connect(
            host="127.0.0.1",
            port=self.db_port,
            dbname=generation["database"],
            user="postgres",
            password=self.config["database"]["owner_password"],
            connect_timeout=5,
            sslmode="disable",
        )

    def runtime_connection(self, generation, role):
        if role not in {"web", "worker"}:
            raise ValueError("Expected a runtime role")
        return psycopg.connect(
            host="127.0.0.1",
            port=self.db_port,
            dbname=generation["database"],
            user=f"px_{self.instance}_{role}",
            password=self.config["database"][role + "_password"],
            connect_timeout=5,
            sslmode="disable",
            options="-c statement_timeout=15000 -c lock_timeout=3000",
            autocommit=True,
        )

    def catalogue_snapshot(self, generation):
        """Compare all stable IDs, versions and private test data without emitting rows."""
        result = {}
        with self.connection(generation) as connection:
            for table in ["core_knowledgenode", "core_problem", "core_problemversion"]:
                rows = connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY id").format(
                        sql.Identifier(table)
                    )
                ).fetchall()
                encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                result[table] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
        return result

    def schema_state(self, generation):
        with self.connection(generation) as connection:
            migrations = set(
                connection.execute("SELECT app, name FROM django_migrations").fetchall()
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                ).fetchall()
            }
        return migrations, tables

    def user_id(self, generation, username):
        with self.connection(generation) as connection:
            row = connection.execute(
                "SELECT id FROM auth_user WHERE username=%s", (username,)
            ).fetchone()
        return row[0] if row else None

    def snapshot(self, generation, usernames):
        """Hash real user rows, private data and completed results; never emit them."""
        tables = {
            "auth_user": "id",
            "core_accountprofile": "user_id",
            "core_draft": "user_id",
            "core_enrollment": "user_id",
            "core_submission": "user_id",
        }
        with self.connection(generation) as connection:
            identities = connection.execute(
                "SELECT id FROM auth_user WHERE username=ANY(%s) ORDER BY id", (usernames,)
            ).fetchall()
            ids = [row[0] for row in identities]
            assert len(ids) == len(usernames)
            result = {}
            for table, key in tables.items():
                rows = connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE {}=ANY(%s) ORDER BY id").format(
                        sql.Identifier(table), sql.Identifier(key)
                    ),
                    (ids,),
                ).fetchall()
                serialized = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                result[table] = {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(serialized).hexdigest(),
                }
            sequences = {}
            for name in ["auth_user_id_seq", "core_draft_id_seq", "core_enrollment_id_seq"]:
                sequences[name] = list(
                    connection.execute(
                        sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(name))
                    ).fetchone()
                )
        return {"tables": result, "sequences": sequences, "user_ids": ids}

    def register(self, label, *, judge=True):
        username, password = label + "-" + uuid.uuid4().hex[:12], secrets.token_urlsafe(32)
        client = Client(self.base, self.context)
        assert client.request("/accounts/register/")[0] == 200
        assert (
            client.request(
                "/accounts/register/",
                {"username": username, "password1": password, "password2": password},
            )[0]
            == 200
        )
        client.login(username, password)
        self.accounts[username] = (client, password)
        cookies = {cookie.name: cookie for cookie in client.cookies}
        assert cookies["sessionid"].secure and cookies["csrftoken"].secure
        assert (
            client.request(
                "/problems/sum-positive/draft/",
                {"source": CORRECT, "version": client.version},
                {"Accept": "application/json"},
            )[0]
            == 200
        )
        assert client.request("/learn/python-to-numerical/enroll/", {"action": "join"})[0] == 200
        if judge:
            self.submit_and_wait(client)
        return username

    def submit_and_wait(self, client):
        status, job = client.submit(CORRECT)
        assert status == 202
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, _, body = client.request(f"/api/submissions/{job['id']}/")
            assert status == 200
            result = json.loads(body)
            if result["status"] == "finished":
                assert result["verdict"] == "accepted", result["verdict"]
                return job["id"]
            time.sleep(0.2)
        raise RuntimeError("Real HTTPS submission did not finish")

    def failed_migration(self, previous, users):
        print(
            "Applying the genuine atomic migration failure and checking automatic rollback",
            flush=True,
        )
        before = self.snapshot(previous, users)
        started = datetime.now(UTC).isoformat()
        process = self.spawn(
            "apply",
            "--release",
            str(self.options.failed_release),
            "--sha256",
            self.options.failed_manifest_sha256,
            log="failed-migration",
        )
        assert process.wait(timeout=900) != 0
        journal = self.journal()
        operation = journal["operation"]
        assert operation["phase"] == "rolled_back"
        assert operation["failure"]["phase"] == "migrating"
        assert journal["current"]["id"] == previous["id"]
        assert journal["current"]["database"] == previous["database"]
        assert operation["exposure_target"] == previous["id"]
        candidate = operation["candidate"]
        assert candidate["release_sha256"] == self.options.failed_manifest_sha256
        assert candidate["id"] != previous["id"]
        # Do not retain raw PostgreSQL logs, which could contain unrelated SQL.
        # Match the ERROR line, not merely the literal in a logged DO statement.
        logs = subprocess.run(
            ["docker", "logs", "--since", started, self.names["pg"]],
            capture_output=True,
            check=True,
            timeout=30,
        )
        marker_seen = bool(
            re.search(
                rb"ERROR:\s+PRAXIMESH_ACCEPTANCE_MIGRATION_FAILED_AFTER_DDL(?:\s|$)",
                logs.stdout + logs.stderr,
            )
        )
        assert marker_seen
        diagnostic_paths = list(self.state_dir.glob("control-failure-*.json"))
        diagnostics = []
        for path in diagnostic_paths:
            assert path.stat().st_mode & 0o077 == 0
            assert path.stat().st_size <= 1024 * 1024
            raw = path.read_text()
            for section, keys in [
                ("database", ["owner_password", "web_password", "worker_password"]),
                ("application", ["secret_key", "worker_secret"]),
            ]:
                assert all(self.config[section][key] not in raw for key in keys)
            diagnostics.append(json.loads(raw))
        assert any(
            entry["command"] == "migrate"
            and "PRAXIMESH_ACCEPTANCE_MIGRATION_FAILED_AFTER_DDL" in entry["stderr"]
            for entry in diagnostics
        ), "The failed management container must leave a private root-cause diagnostic"
        with self.connection(candidate) as connection:
            assert (
                connection.execute(
                    "SELECT to_regclass('public.core_acceptance_migration_probe')"
                ).fetchone()[0]
                is None
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM django_migrations WHERE app='core' AND name=%s",
                    (failure_migration_name(candidate["manifest"]),),
                ).fetchone()[0]
                == 0
            )
        assert not docker(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            "label=org.praximesh.site=" + self.site_id,
            "--filter",
            "label=org.praximesh.generation=" + candidate["id"],
        ).strip()
        restored = self.snapshot(previous, users)
        assert restored["tables"] == before["tables"]
        for sequence, state in before["sequences"].items():
            assert restored["sequences"][sequence][0] >= state[0]
        actual = self.proxy.status()
        info = json.loads(docker("container", "inspect", previous["web"]))[0]
        assert actual["mode"] == "active"
        assert info["NetworkSettings"]["Ports"]["8000/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(actual["port"])}
        ]
        assert operation["backup"]["backup_bytes"] > 0
        backup_path = self.state_dir / "backups" / (candidate["id"] + ".dump")
        with backup_path.open("rb") as stream:
            assert (
                hashlib.file_digest(stream, "sha256").hexdigest()
                == operation["backup"]["backup_sha256"]
            )
        self.submit_and_wait(self.accounts[users[0]][0])
        self.result.update(
            schema_failure_rollback_tested=True,
            failed_release_sha256=self.options.failed_manifest_sha256,
            failed_migration_name=failure_migration_name(candidate["manifest"]),
            migration_failure_observed_after_real_DDL=marker_seen,
            atomic_probe_table_and_migration_record_absent=True,
            failed_candidate_never_exposed=True,
            migration_rollback_preserved_data_and_real_judging=True,
            private_migration_diagnostic_retained=True,
        )

    def browser(self, *, problem_feedback=False):
        environment = dict(
            os.environ, BROWSER_URL=self.base, BROWSER_TEST_SPKI=self.pin, BROWSER_JUDGE="1"
        )
        with (self.options.artifacts / "browser.log").open("wb") as stream:
            subprocess.run(
                [sys.executable, str(ROOT / "tests/browser_smoke.py")],
                cwd=self.directory,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        reviewer = "community-reviewer-" + uuid.uuid4().hex[:12]
        password = secrets.token_urlsafe(32)
        client = Client(self.base, self.context)
        assert client.request("/accounts/register/")[0] == 200
        assert (
            client.request(
                "/accounts/register/",
                {"username": reviewer, "password1": password, "password2": password},
            )[0]
            == 200
        )
        # Bootstrap only a permission in this owned disposable database. Every
        # subsequent content/moderation transition goes through real HTTPS forms.
        generation = self.journal()["current"]
        with self.connection(generation) as connection:
            identity = connection.execute(
                "SELECT id, is_staff, is_superuser FROM auth_user WHERE username=%s",
                (reviewer,),
            ).fetchone()
            assert identity and identity[1:] == (False, False)
            permission = connection.execute(
                "SELECT p.id FROM auth_permission p JOIN django_content_type c "
                "ON c.id=p.content_type_id WHERE c.app_label='community' "
                "AND p.codename='moderate_content'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO auth_user_user_permissions (user_id, permission_id) VALUES (%s,%s)",
                (identity[0], permission),
            )
        environment.update(BROWSER_MODERATOR_USERNAME=reviewer, BROWSER_MODERATOR_PASSWORD=password)
        script = "browser_problem_feedback" if problem_feedback else "browser_community"
        with (self.options.artifacts / (script + ".log")).open("wb") as stream:
            subprocess.run(
                [sys.executable, str(ROOT / "tests" / (script + ".py"))],
                cwd=self.directory,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        with self.connection(generation) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM auth_user WHERE username=%s", (reviewer,)
                ).fetchone()[0]
                == 0
            )
        self.result[
            "problem_feedback_via_https_browser"
            if problem_feedback
            else "community_moderation_via_https_browser"
        ] = True
        receipt = (
            self.directory
            / "test-results"
            / ("problem-feedback-browser.json" if problem_feedback else "community-browser.json")
        )
        community_result = json.loads(receipt.read_text())
        assert (
            community_result["passed"]
            and community_result["https" if problem_feedback else "https_transport_tested"]
        )
        (self.options.artifacts / receipt.name).write_bytes(receipt.read_bytes())
        for screenshot in (self.directory / "test-results").glob("*.png"):
            (self.options.artifacts / screenshot.name).write_bytes(screenshot.read_bytes())

    def interrupted_apply(self, checkpoint, expected_phase):
        process = self.spawn(*self.apply_args(), log=checkpoint, interrupt=checkpoint)
        marker = self.directory / (checkpoint + ".checkpoint")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Coordinator exited before the requested real interruption")
            if marker.exists():
                recorded = json.loads(marker.read_text())
                status = Path(f"/proc/{process.pid}/status").read_text()
                state = next(line for line in status.splitlines() if line.startswith("State:"))
                if "T (stopped)" in state:
                    assert recorded == {"pid": process.pid, "phase": expected_phase}
                    assert self.journal()["operation"]["phase"] == expected_phase
                    return process
            time.sleep(0.1)
        raise RuntimeError("Coordinator did not reach the real interruption checkpoint")

    def migration_rehearsal(self):
        print("Testing initial HTTPS deployment and a genuine failed migration", flush=True)
        self.run(*self.apply_args(initial=True), log="initial")
        first = self.journal()["current"]
        self.browser()
        user = self.register("before")
        self.failed_migration(first, [user])
        self.result.update(
            scope="initial deployment and failed migration; synthetic data; loopback HTTPS",
            browser_real_https_run_wrong_accepted=True,
            generations_attempted=2,
            completed_at=datetime.now(UTC).isoformat(),
        )

    def feature_roles(self, generation, username):
        """Real runtime credentials, SQL and rollback; no owner-role impersonation."""
        user_id = self.user_id(generation, username)
        with self.connection(generation) as connection:
            version_id = connection.execute(
                "SELECT current_id FROM core_problem WHERE id='sum-positive'"
            ).fetchone()[0]
            assert all(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
                for table in UPGRADE_TABLES
            ), "An initial feature migration must start with empty private feature tables"
        now = datetime.now(UTC)
        post_id, comment_id = uuid.uuid4(), uuid.uuid4()
        writing = {
            "author_id": user_id,
            "body": "Disposable role probe",
            "state": "draft",
            "revision": 1,
            "moderation_note": "",
            "created_at": now,
            "updated_at": now,
        }
        rows = {
            "learning_bookmark": {
                "user_id": user_id,
                "problem_id": "sum-positive",
                "created_at": now,
            },
            "learning_note": {
                "user_id": user_id,
                "title": "Role probe",
                "body": "Disposable role probe",
                "version_id": version_id,
                "node_id": None,
                "created_at": now,
                "updated_at": now,
            },
            "learning_tutorialreading": {
                "user_id": user_id,
                "tutorial_id": "tutorial-functions",
                "completed_at": now,
                "content_digest": "0" * 64,
            },
            "community_post": {
                **writing,
                "id": post_id,
                "kind": "solution",
                "title": "Role probe",
                "version_id": version_id,
            },
            "community_comment": {**writing, "id": comment_id, "post_id": post_id},
            "community_report": {
                "id": uuid.uuid4(),
                "reporter_id": user_id,
                "post_id": post_id,
                "comment_id": None,
                "reason": "Role probe",
                "status": "open",
                "resolution": "",
                "resolved_by_id": None,
                "created_at": now,
                "updated_at": now,
            },
            "community_appeal": {
                "id": uuid.uuid4(),
                "author_id": user_id,
                "post_id": post_id,
                "comment_id": None,
                "target_revision": 1,
                "reason": "Role probe",
                "status": "open",
                "resolution": "",
                "resolved_by_id": None,
                "created_at": now,
                "updated_at": now,
            },
            "community_moderationevent": {
                "actor_id": user_id,
                "target_kind": "post",
                "target_id": post_id,
                "target_revision": 1,
                "action": "role_probe",
                "created_at": now,
            },
        }
        assert set(rows) == set(UPGRADE_TABLES)
        allowed = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        privileges = allowed | {"TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"}
        for role in ["web", "worker"]:
            with self.runtime_connection(generation, role) as connection:
                assert (
                    connection.execute("SELECT current_user").fetchone()[0]
                    == f"px_{self.instance}_{role}"
                )
                assert (
                    connection.execute(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                        "FROM pg_roles WHERE rolname=current_user"
                    ).fetchone()
                    == (False,) * 5
                )
                assert (
                    connection.execute(
                        "SELECT has_schema_privilege(current_user, 'public', 'CREATE'), "
                        "has_database_privilege(current_user, current_database(), 'CREATE'), "
                        "has_database_privilege(current_user, current_database(), 'TEMP')"
                    ).fetchone()
                    == (False,) * 3
                )
                for table in UPGRADE_TABLES:
                    for privilege in sorted(privileges):
                        actual, delegable = connection.execute(
                            "SELECT has_table_privilege(current_user, %s, %s), "
                            "has_table_privilege(current_user, %s, %s)",
                            (table, privilege, table, privilege + " WITH GRANT OPTION"),
                        ).fetchone()
                        assert actual == (role == "web" and privilege in allowed)
                        assert not delegable
                if role == "web":
                    # Exercise generated identities as well as table permissions.
                    # The complete transaction is rolled back even on success.
                    with connection.transaction(force_rollback=True):
                        inserted = []
                        for table, values in rows.items():
                            identifier = connection.execute(
                                sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                                    sql.Identifier(table),
                                    sql.SQL(", ").join(map(sql.Identifier, values)),
                                    sql.SQL(", ").join(sql.Placeholder() for _ in values),
                                ),
                                tuple(values.values()),
                            ).fetchone()[0]
                            assert connection.execute(
                                sql.SQL("SELECT id FROM {} WHERE id=%s").format(
                                    sql.Identifier(table)
                                ),
                                (identifier,),
                            ).fetchone() == (identifier,)
                            assert (
                                connection.execute(
                                    sql.SQL("UPDATE {} SET id=id WHERE id=%s").format(
                                        sql.Identifier(table)
                                    ),
                                    (identifier,),
                                ).rowcount
                                == 1
                            )
                            inserted.append((table, identifier))
                        for table, identifier in reversed(inserted):
                            assert (
                                connection.execute(
                                    sql.SQL("DELETE FROM {} WHERE id=%s").format(
                                        sql.Identifier(table)
                                    ),
                                    (identifier,),
                                ).rowcount
                                == 1
                            )
                else:
                    # ACL inspection is supplemented by actual refused SQL. If a
                    # refusal fails, its surrounding transaction still rolls back.
                    for table in UPGRADE_TABLES:
                        for statement in [
                            "SELECT id FROM {} LIMIT 0",
                            "INSERT INTO {} DEFAULT VALUES",
                            "UPDATE {} SET id=id WHERE FALSE",
                            "DELETE FROM {} WHERE FALSE",
                        ]:
                            try:
                                with connection.transaction(force_rollback=True):
                                    connection.execute(
                                        sql.SQL(statement).format(sql.Identifier(table))
                                    )
                            except psycopg.errors.InsufficientPrivilege:
                                pass
                            else:
                                raise AssertionError(
                                    "Worker unexpectedly accessed a private feature table"
                                )
        with self.connection(generation) as connection:
            for table in UPGRADE_TABLES:
                assert (
                    connection.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone()[0]
                    == 0
                )
        return {
            "tables": list(UPGRADE_TABLES),
            "web_real_crud_rolled_back": True,
            "worker_real_crud_denied": True,
            "no_extra_table_privileges_or_grant_options": True,
            "no_role_administration_or_schema_database_creation": True,
        }

    def upgraded_http(self, generation, username):
        client, _ = self.accounts[username]
        for endpoint in [
            "/learning/",
            "/learning/notes/",
            "/learning/reading/",
            "/community/mine/",
        ]:
            status, headers, _ = client.request(endpoint)
            assert status == 200 and headers["Cache-Control"] == "no-store"
        assert client.request("/learning/bookmarks/sum-positive/", {"action": "save"})[0] == 200
        assert (
            client.request(
                "/learning/notes/new/",
                {
                    "title": "Upgrade private note",
                    "body": "Retained learner's new note",
                    "version": client.version,
                },
            )[0]
            == 200
        )
        assert (
            client.request("/learning/reading/tutorial-functions/", {"action": "complete"})[0]
            == 200
        )
        assert (
            client.request(
                "/community/new/",
                {
                    "kind": "solution",
                    "title": "Upgrade private solution",
                    "body": "Private draft",
                    "version": client.version,
                },
            )[0]
            == 200
        )
        status, _, body = client.request("/accounts/export/", {})
        assert status == 200
        exported = json.loads(body)
        assert len(exported["learning"]["bookmarks"]) == 1
        assert len(exported["learning"]["notes"]) == 1
        assert len(exported["learning"]["reading"]) == 1
        with self.connection(generation) as connection:
            post_id, state = connection.execute(
                "SELECT id, state FROM community_post WHERE author_id=%s",
                (self.user_id(generation, username),),
            ).fetchone()
            assert state == "draft"
        assert Client(self.base, self.context).request(f"/community/{post_id}/")[0] == 404
        self.submit_and_wait(client)

    def upgrade_rehearsal(self):
        print(
            "Installing the verified Phase A release and creating real private data and AC",
            flush=True,
        )
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        assert previous["release_sha256"] == self.options.previous_manifest_sha256
        old_migrations, old_tables = self.schema_state(previous)
        assert not {app for app, _ in old_migrations}.intersection({"learning", "community"})
        assert not set(UPGRADE_TABLES).intersection(old_tables)
        catalogue = self.catalogue_snapshot(previous)
        assert catalogue["core_problem"]["rows"] == 8, (
            "Expected the frozen Phase A eight-problem catalogue"
        )
        username = self.register("upgrade-before")
        before = self.snapshot(previous, [username])
        assert before["tables"]["core_draft"]["rows"] == 1
        assert before["tables"]["core_submission"]["rows"] == 1

        print(
            "Applying the B1 release with real migrations and no catalogue publication", flush=True
        )
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert journal["operation"]["phase"] == "active"
        assert upgraded["release_sha256"] == self.options.manifest_sha256
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert UPGRADE_MIGRATIONS <= new_migrations - old_migrations
        assert old_migrations <= new_migrations
        assert set(UPGRADE_TABLES) <= new_tables - old_tables
        restored = self.snapshot(upgraded, [username])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        for sequence, value in before["sequences"].items():
            assert restored["sequences"][sequence][0] >= value[0]
        assert self.catalogue_snapshot(upgraded) == catalogue
        backup = journal["operation"]["backup"]
        assert backup["backup_bytes"] > 0 and backup["sequences"] > 0
        permissions = self.feature_roles(upgraded, username)
        self.upgraded_http(upgraded, username)
        self.browser()
        assert self.catalogue_snapshot(upgraded) == catalogue
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_user_draft_and_formal_AC_preserved=True,
            original_catalogue_preserved=catalogue,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            upgraded_private_features_and_export_via_https=True,
            retained_user_new_real_https_submission_accepted=True,
            browser_real_https_run_wrong_accepted=True,
            backup_restore=backup,
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )

    def exercise(self):
        print(
            "Applying the first immutable generation and exercising real HTTPS judging", flush=True
        )
        self.run(*self.apply_args(initial=True), log="initial")
        first = self.journal()["current"]
        assert self.journal()["operation"]["phase"] == "active"
        assert self.proxy.status()["mode"] == "active"
        self.browser()
        first_user = self.register("before")
        before = self.snapshot(first, [first_user])
        print(
            "Killing the dedicated proxy and restarting from its durable active configuration",
            flush=True,
        )
        intended = self.proxy.config_path.read_bytes()
        stop(self.proxy_process)
        assert self.proxy_process.returncode == -signal.SIGKILL
        assert self.proxy.admin_socket.exists()
        self.proxy_process = self.spawn("serve-proxy", log="proxy-restarted")
        self.wait_proxy("active")
        assert self.proxy.config_path.read_bytes() == intended
        assert Client(self.base, self.context).request("/health/")[0] == 200
        assert self.snapshot(first, [first_user]) == before
        self.result["proxy_sigkill_stale_socket_restart_preserved_active_config"] = True

        print(
            "Replacing the generation and verifying preserved user rows and sequence state",
            flush=True,
        )
        self.run(*self.apply_args(), log="replacement")
        journal = self.journal()
        second = journal["current"]
        assert second["id"] != first["id"] and second["database"] != first["database"]
        restored = self.snapshot(second, [first_user])
        assert restored["tables"] == before["tables"]
        # Deployment verification creates and deletes its own user; its sequence
        # legitimately advances. Neither identity nor sequence may regress.
        for sequence, value in before["sequences"].items():
            assert restored["sequences"][sequence][0] >= value[0]
        second_user = self.register("after", judge=False)
        assert self.user_id(second, second_user) > before["sequences"]["auth_user_id_seq"][0]
        backup = journal["operation"]["backup"]
        assert backup["backup_bytes"] > 0 and backup["sequences"] > 0
        self.result.update(
            clean_replacement_preserved_user_rows=True,
            restored_sequence_allows_new_identity=True,
            backup_restore=backup,
            browser_real_https_run_wrong_accepted=True,
        )
        if self.options.failed_release:
            self.failed_migration(second, [first_user, second_user])
        stable = self.snapshot(second, [first_user, second_user])["tables"]

        print(
            "Killing a real pre-exposure coordinator and recovering the previous generation",
            flush=True,
        )
        interrupted = self.interrupted_apply("before_validation", "validating")
        unfinished = self.journal()["operation"]["candidate"]
        assert self.proxy.status()["mode"] == "maintenance"
        stop(interrupted)
        assert interrupted.returncode == -signal.SIGKILL
        self.run("recover", log="recover-previous")
        journal = self.journal()
        assert journal["operation"]["phase"] == "rolled_back"
        assert journal["current"]["id"] == second["id"]
        assert self.snapshot(journal["current"], [first_user, second_user])["tables"] == stable
        for role in ["web", "worker"]:
            info = json.loads(docker("container", "inspect", unfinished[role]))[0]
            assert not info["State"]["Running"]
        self.result["interrupted_before_exposure_restored_previous"] = True

        print(
            "Writing through the actually exposed candidate before interruption and recovery",
            flush=True,
        )
        interrupted = self.interrupted_apply("after_exposure", "exposing")
        journal = self.journal()
        exposed = journal["current"]
        assert journal["operation"]["exposure_target"] == exposed["id"]
        actual = self.proxy.status()
        info = json.loads(docker("container", "inspect", exposed["web"]))[0]
        bound = info["NetworkSettings"]["Ports"]["8000/tcp"]
        assert bound == [{"HostIp": "127.0.0.1", "HostPort": str(actual["port"])}]
        assert actual["mode"] == "active"
        new_user = self.register("exposed")
        assert self.user_id(second, new_user) is None
        deleted_client, deleted_password = self.accounts[second_user]
        assert (
            deleted_client.request(
                "/accounts/delete/", {"password": deleted_password, "confirm": "DELETE"}
            )[0]
            == 200
        )
        assert self.user_id(exposed, second_user) is None
        assert self.user_id(second, second_user) is not None
        exposed_snapshot = self.snapshot(exposed, [first_user, new_user])["tables"]
        stop(interrupted)
        assert interrupted.returncode == -signal.SIGKILL
        self.run("recover", log="recover-current")
        final = self.journal()
        assert final["operation"]["phase"] == "active"
        assert final["current"]["id"] == exposed["id"]
        assert final["current"]["database"] == exposed["database"]
        assert self.snapshot(final["current"], [first_user, new_user])["tables"] == exposed_snapshot
        assert self.user_id(final["current"], second_user) is None
        assert self.proxy.status()["mode"] == "active"
        assert Client(self.base, self.context).request("/health/")[0] == 200
        self.restart_existing(final["current"], [first_user, new_user])
        assert self.user_id(final["current"], second_user) is None
        self.result.update(
            interrupted_after_exposure_preserved_new_writes=True,
            post_exposure_account_deletion_not_resurrected=True,
            recovery_kept_exposed_database=True,
            no_journal_or_verdict_edits=True,
            generations_attempted=4 + bool(self.options.failed_release),
            completed_at=datetime.now(UTC).isoformat(),
        )

    def worker_restart(self):
        """Focused real regression for recovery during a worker auto-restart."""
        print("Applying one immutable generation for focused worker restart recovery", flush=True)
        self.run(*self.apply_args(initial=True), log="initial")
        generation = self.journal()["current"]
        assert self.journal()["operation"]["phase"] == "active"
        user = self.register("restart")
        self.restart_existing(generation, [user])
        self.result.update(
            generations_attempted=1,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )

    def restart_existing(self, generation, users):
        print(
            "Killing the owned worker controller and recovering during its fresh probes", flush=True
        )
        before = self.snapshot(generation, users)["tables"]
        worker = generation["worker"]
        initial = json.loads(docker("container", "inspect", worker))[0]
        labels = initial["Config"]["Labels"]
        assert labels.get("org.praximesh.site") == self.site_id
        assert labels.get("org.praximesh.generation") == generation["id"]
        assert labels.get("org.praximesh.role") == "worker"
        assert initial["State"]["Running"] and not initial["HostConfig"].get("PidMode")
        assert initial["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
        observer = Deployment(self.config, self.state_dir)
        observer.state = self.journal()
        assert observer.ready(generation)
        killed = subprocess.run(
            ["docker", "exec", worker, "python", "-c", KILL_WORKER, worker],
            capture_output=True,
            timeout=30,
        )
        assert killed.returncode in {0, 137}
        assert b"OWNED_WORKER_SIGKILL" in killed.stdout
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            restarted = json.loads(docker("container", "inspect", worker))[0]
            if (
                restarted["RestartCount"] > initial["RestartCount"]
                and restarted["State"]["Running"]
                and not restarted["State"]["Restarting"]
            ):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Owned worker did not automatically restart after controller death")
        assert restarted["State"]["StartedAt"] != initial["State"]["StartedAt"]
        entry_ready = observer.ready(generation)
        assert entry_ready is False, (
            "Recovery must enter while the new startup probes are incomplete"
        )
        count_at_entry = restarted["RestartCount"]
        assert count_at_entry >= 1
        self.run("recover", log="recover-worker-restart")
        final = self.journal()
        assert final["operation"]["phase"] == "active"
        assert final["current"]["id"] == generation["id"]
        assert final["current"]["database"] == generation["database"]
        assert self.snapshot(final["current"], users)["tables"] == before
        observer.state = final
        assert observer.ready(final["current"])
        assert self.proxy.status()["mode"] == "active"
        # Recovery deliberately stops and starts services after draining. Docker
        # may reset RestartCount on that manual start; record, do not assume it.
        after = json.loads(docker("container", "inspect", worker))[0]
        assert after["State"]["Running"]
        self.submit_and_wait(self.accounts[users[0]][0])
        self.result.update(
            worker_auto_restart_recovery_tested=True,
            worker_restart_count_before_fault=initial["RestartCount"],
            worker_restart_count_at_recovery=count_at_entry,
            worker_ready_at_recovery_entry=entry_ready,
            worker_restart_count_after_recovery=after["RestartCount"],
            worker_recovery_preserved_exposed_database_and_users=True,
            worker_recovery_real_https_submission_accepted=True,
        )

    def failure_diagnostic(self):
        """Keep only bounded operational fields; never persist the private journal."""
        try:
            operation = self.journal().get("operation") or {}
            diagnostic = {"phase": operation.get("phase"), "failure": operation.get("failure")}
            encoded = json.dumps(diagnostic, ensure_ascii=True)
            if len(encoded) > 4096:
                diagnostic["failure"] = "Diagnostic exceeded its bounded size"
            (self.options.artifacts / "operation-failure.json").write_text(
                json.dumps(diagnostic, indent=2) + "\n"
            )
        except (OSError, ValueError, TypeError):
            pass

    def cleanup(self):
        errors = []
        for process in self.processes:
            try:
                stop(process)
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(type(error).__name__)
        for stream in self.log_streams:
            stream.close()
        labels = [LABEL + "=" + self.token]
        if self.site_id:
            labels.append("org.praximesh.site=" + self.site_id)
        for label in labels:
            try:
                names = (
                    docker("container", "ls", "--all", "--quiet", "--filter", "label=" + label)
                    .decode()
                    .splitlines()
                )
                for name in names:
                    info = json.loads(docker("container", "inspect", name))[0]
                    key, value = label.split("=", 1)
                    assert info["Config"]["Labels"].get(key) == value
                    # Only Web/worker logs; no owner credentials or database dumps
                    # are copied out of the private temporary directory.
                    if key == "org.praximesh.site" and info["Config"]["Labels"].get(
                        "org.praximesh.role"
                    ) in {"web", "worker"}:
                        logs = subprocess.run(
                            ["docker", "logs", name], capture_output=True, timeout=15
                        )
                        (self.options.artifacts / (info["Name"].lstrip("/") + ".log")).write_bytes(
                            logs.stdout + logs.stderr
                        )
                    docker("rm", "--force", "--volumes", name)
            except (OSError, subprocess.SubprocessError, AssertionError) as error:
                errors.append(type(error).__name__)
        for resource in ["volume", "network"]:
            try:
                for name in self.labelled(resource, LABEL + "=" + self.token):
                    docker(resource, "rm", name)
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(type(error).__name__)
        remaining = {}
        for resource in ["container", "network", "volume"]:
            for label in labels:
                try:
                    args = [
                        resource,
                        "ls",
                        *(["--all"] if resource == "container" else []),
                        "--quiet",
                        "--filter",
                        "label=" + label,
                    ]
                    found = docker(*args).decode().splitlines()
                    if found:
                        remaining[resource + ":" + label] = found
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(type(error).__name__)
        self.result["cleanup"] = {
            "passed": not errors and not remaining,
            "errors": errors,
            "remaining_owned_resources": remaining,
            "database_named_volume_removed": not errors and not remaining,
            "local_processes_terminal": all(
                process.poll() is not None for process in self.processes
            ),
        }
        if errors or remaining:
            raise RuntimeError("Owned rehearsal cleanup could not be fully confirmed")


def parse_options(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument(
        "--previous-release", type=Path, help="Verified previous release, for --upgrade-only"
    )
    parser.add_argument(
        "--previous-manifest-sha256",
        help="Independently trusted SHA-256 of the previous release manifest",
    )
    parser.add_argument("--failed-release", type=Path)
    parser.add_argument("--failed-manifest-sha256")
    parser.add_argument("--worker-restart-only", action="store_true")
    parser.add_argument("--migration-only", action="store_true")
    parser.add_argument(
        "--upgrade-only",
        action="store_true",
        help=(
            "Rehearse A-to-B1, B1-to-B2, B2-to-C or C-to-D schema preservation; "
            "later stages explicitly publish reviewed content after verifying preservation"
        ),
    )
    parser.add_argument("--caddy", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "test-results/deployment")
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(arguments)
    if bool(options.previous_release) != bool(options.previous_manifest_sha256):
        parser.error("--previous-release and --previous-manifest-sha256 must be supplied together")
    if options.upgrade_only:
        if not options.previous_release:
            parser.error("--upgrade-only requires the previous release and its trusted digest")
        if options.worker_restart_only or options.migration_only or options.failed_release:
            parser.error("--upgrade-only excludes worker restart and failed migration modes")
        if options.previous_manifest_sha256 == options.manifest_sha256:
            parser.error("A schema upgrade requires two different verified releases")
    elif options.previous_release:
        parser.error("Previous release options require --upgrade-only")
    if bool(options.failed_release) != bool(options.failed_manifest_sha256):
        parser.error("--failed-release and --failed-manifest-sha256 must be supplied together")
    if options.worker_restart_only and options.failed_release:
        parser.error("--worker-restart-only does not run the optional migration fixture")
    if options.migration_only and (not options.failed_release or options.worker_restart_only):
        parser.error(
            "--migration-only requires a failed release and excludes --worker-restart-only"
        )
    options.release, options.caddy = options.release.resolve(), options.caddy.resolve()
    if options.previous_release:
        options.previous_release = options.previous_release.resolve()
        if options.previous_release == options.release:
            parser.error("A schema upgrade requires distinct release directories")
    options.artifacts = options.artifacts.resolve()
    return options


def validate_upgrade_manifests(previous, target):
    """Preflight contract only. It cannot prove that a live migration succeeds."""
    paths = {f"{app}/migrations/{name}.py" for app, name in UPGRADE_MIGRATIONS}
    b2_paths = {f"{app}/migrations/{name}.py" for app, name in B2_MIGRATIONS}
    c_paths = {f"{app}/migrations/{name}.py" for app, name in C_MIGRATIONS}
    d_paths = {f"{app}/migrations/{name}.py" for app, name in D_MIGRATIONS}
    f_paths = {f"{app}/migrations/{name}.py" for app, name in F_MIGRATIONS}
    if paths | b2_paths | c_paths | d_paths <= previous["source_files"].keys():
        if f_paths.intersection(previous["source_files"]):
            raise ValueError("The previous release must precede the F schema")
        if not f_paths <= target["source_files"].keys():
            raise ValueError("The target release must contain all F migrations")
        for path, digest in previous["source_files"].items():
            if "/migrations/" in path and target["source_files"].get(path) != digest:
                raise ValueError("An upgrade must preserve existing migration sources")
        if previous["images"]["web"] == target["images"]["web"]:
            raise ValueError("A schema upgrade must exercise a different Web image")
        return "f"
    if paths | b2_paths | c_paths <= previous["source_files"].keys():
        if d_paths.intersection(previous["source_files"]):
            raise ValueError("The previous release must precede the D schema")
        if not d_paths <= target["source_files"].keys():
            raise ValueError("The target release must contain all D migrations")
        for path, digest in previous["source_files"].items():
            if "/migrations/" in path and target["source_files"].get(path) != digest:
                raise ValueError("An upgrade must preserve existing migration sources")
        if previous["images"]["web"] == target["images"]["web"]:
            raise ValueError("A schema upgrade must exercise a different Web image")
        return "d"
    if paths | b2_paths <= previous["source_files"].keys():
        if c_paths.intersection(previous["source_files"]):
            raise ValueError("The previous release must precede the C schema")
        if not c_paths <= target["source_files"].keys():
            raise ValueError("The target release must contain all C migrations")
        for path, digest in previous["source_files"].items():
            if "/migrations/" in path and target["source_files"].get(path) != digest:
                raise ValueError("An upgrade must preserve existing migration sources")
        if previous["images"]["web"] == target["images"]["web"]:
            raise ValueError("A schema upgrade must exercise a different Web image")
        return "c"
    if paths <= previous["source_files"].keys():
        if any(path in previous["source_files"] for path in b2_paths):
            raise ValueError("The previous release must precede the B2 schema")
        if not b2_paths <= target["source_files"].keys():
            raise ValueError("The target release must contain all B2 migrations")
        for path, digest in previous["source_files"].items():
            if "/migrations/" in path and target["source_files"].get(path) != digest:
                raise ValueError("An upgrade must preserve existing migration sources")
        if previous["images"]["web"] == target["images"]["web"]:
            raise ValueError("A schema upgrade must exercise a different Web image")
        return "b2"
    if any(
        path.startswith(("learning/migrations/", "community/migrations/"))
        and path.endswith(".py")
        and not path.endswith("/__init__.py")
        for path in previous["source_files"]
    ):
        raise ValueError("The previous release must precede the learning/community schema")
    if not paths <= target["source_files"].keys():
        raise ValueError("The target release must contain both B1 initial migrations")
    if previous["images"]["web"] == target["images"]["web"]:
        raise ValueError("A schema upgrade must exercise a different Web image")
    return "b1"


def failure_migration_name(manifest):
    """Identify exactly one numbered fixture in an already verified release."""
    names = []
    for path in manifest["source_files"]:
        if match := re.fullmatch(r"core/migrations/([0-9]{4}_acceptance_atomic_failure)\.py", path):
            names.append(match[1])
    if len(names) != 1 or int(names[0][:4]) < 3:
        raise ValueError(
            "Expected exactly one explicitly constructed atomic migration failure fixture"
        )
    return names[0]


def main():
    options = parse_options()
    if options.failed_release:
        options.failed_release = options.failed_release.resolve()
        failed = verify(options.failed_release, options.failed_manifest_sha256)
        failure_migration_name(failed)
    options.artifacts.mkdir(parents=True, exist_ok=True)
    output = options.output or options.artifacts / "deployment-acceptance.json"
    output.unlink(missing_ok=True)
    manifest = verify(options.release, options.manifest_sha256)
    previous_manifest = None
    rehearsal_type = Rehearsal
    if options.upgrade_only:
        previous_manifest = verify(options.previous_release, options.previous_manifest_sha256)
        upgrade_kind = validate_upgrade_manifests(previous_manifest, manifest)
        if upgrade_kind == "b2":
            from scripts.deploy_b2_acceptance import B2Rehearsal

            rehearsal_type = B2Rehearsal
        elif upgrade_kind == "c":
            from scripts.deploy_c_acceptance import CRehearsal

            rehearsal_type = CRehearsal
        elif upgrade_kind == "d":
            from scripts.deploy_d_acceptance import DRehearsal

            rehearsal_type = DRehearsal
        elif upgrade_kind == "f":
            from scripts.deploy_f_acceptance import FRehearsal

            rehearsal_type = FRehearsal
        docker(
            "image", "load", "--input", str(options.previous_release / "images.tar"), timeout=900
        )
        inspect_images(previous_manifest)
    docker("image", "load", "--input", str(options.release / "images.tar"), timeout=900)
    inspect_images(manifest)
    version = command(str(options.caddy), "version").decode().strip()
    if not version.startswith("v2.11.7 "):
        raise ValueError("Use the pinned Caddy 2.11.7 executable")
    with tempfile.TemporaryDirectory(prefix="px-deploy-") as temporary:
        rehearsal = rehearsal_type(options, Path(temporary))
        rehearsal.result.update(
            source_id=manifest["source_id"], images=manifest["images"], caddy=version
        )
        if previous_manifest is not None:
            rehearsal.result.update(
                previous_source_id=previous_manifest["source_id"],
                previous_images=previous_manifest["images"],
            )
        try:
            rehearsal.start()
            if options.upgrade_only:
                rehearsal.upgrade_rehearsal()
            elif options.worker_restart_only:
                rehearsal.worker_restart()
            elif options.migration_only:
                rehearsal.migration_rehearsal()
            else:
                rehearsal.exercise()
            rehearsal.result["passed"] = True
        except BaseException as error:
            rehearsal.result["failure_type"] = type(error).__name__
            rehearsal.failure_diagnostic()
            raise
        finally:
            try:
                rehearsal.cleanup()
            except BaseException:
                rehearsal.result["passed"] = False
                raise
            finally:
                output.write_text(json.dumps(rehearsal.result, indent=2) + "\n")
    print(json.dumps(rehearsal.result, indent=2), flush=True)


if __name__ == "__main__":
    main()
