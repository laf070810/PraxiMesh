"""Single-host, journalled release activation with database-preserving recovery.

The operator supplies a dedicated PostgreSQL container and networks. This tool
never changes a shared daemon, publishes an image, or drops a database/backup.
"""

import argparse
import copy
import fcntl
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError  # noqa: E402
from scripts.deploy_proxy import DeploymentProxy, ProxyError  # noqa: E402
from scripts.release import inspect_images, verify  # noqa: E402

CONTROL_DIAGNOSTIC_STREAM_BYTES = 64 * 1024
CONTROL_DIAGNOSTIC_MAX_BYTES = 1024 * 1024
CONTROL_COMMANDS = {
    "check",
    "content_import",
    "deployment_check",
    "judge_admission",
    "maintain",
    "migrate",
}


class DeploymentError(RuntimeError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise DeploymentError("Configuration must be a regular private file")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeploymentError("Configuration must be owned by this user with mode 0600")


def configuration(path):
    private_file(path)
    config = json.loads(path.read_text())
    required = {"format", "instance", "database", "networks", "application", "proxy"}
    if (
        not isinstance(config, dict)
        or not required <= config.keys()
        or set(config) - required - {"health_ca"}
    ):
        raise DeploymentError("Unexpected deployment configuration fields")
    if (
        type(config["format"]) is not int
        or config["format"] != 1
        or not isinstance(config["instance"], str)
        or not re.fullmatch(r"[a-z][a-z0-9]{0,19}", config["instance"])
    ):
        raise DeploymentError("Unsupported format or instance identifier")
    if any(not isinstance(config[key], dict) for key in ["database", "networks", "application"]):
        raise DeploymentError("Database, network and application settings must be objects")
    database = config["database"]
    if set(database) != {
        "container",
        "owner_user",
        "owner_password",
        "web_password",
        "worker_password",
    } or any(
        not isinstance(value, str) or not value or "\0" in value for value in database.values()
    ):
        raise DeploymentError("Invalid database configuration fields")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database["owner_user"]):
        raise DeploymentError("Invalid database owner identifier")
    passwords = [database[key] for key in ["owner_password", "web_password", "worker_password"]]
    if (
        any(len(value) < 24 or value.startswith("REPLACE") for value in passwords)
        or len(set(passwords)) != 3
    ):
        raise DeploymentError(
            "Generate three distinct database passwords of at least 24 characters"
        )
    if set(config["networks"]) != {"backend", "frontend"}:
        raise DeploymentError("Both deployment networks must be explicitly named")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value)
        for value in [database["container"], *config["networks"].values()]
    ):
        raise DeploymentError("Invalid container or network identifier")
    if set(config["application"]) != {"secret_key", "worker_secret", "registration_open"}:
        raise DeploymentError("Application secrets and registration policy must be explicit")
    app = config["application"]
    if type(app["registration_open"]) is not bool:
        raise DeploymentError("registration_open must be boolean")
    if any(
        not isinstance(app[key], str)
        or len(app[key]) < 50
        or app[key].startswith("REPLACE")
        or "\0" in app[key]
        for key in ["secret_key", "worker_secret"]
    ):
        raise DeploymentError("Generate independent application secrets of at least 50 characters")
    if app["secret_key"] == app["worker_secret"]:
        raise DeploymentError("Web and worker secrets must differ")
    return config


class Deployment:
    def __init__(self, config, state_dir):
        self.config = config
        self.directory = Path(state_dir).absolute()
        self.proxy = DeploymentProxy(config["proxy"], self.directory)
        self._database = None
        self.path = self.directory / "deployment.json"
        self.config_digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.state = None

    @property
    def database(self):
        # Status and proxy maintenance must remain usable during a DB outage.
        if not self.state:
            raise DeploymentError("Run init before accessing deployment databases")
        if self._database is None:
            self._database = DeploymentDatabase(
                dict(
                    self.config["database"],
                    instance=self.config["instance"],
                    site_id=self.state["site_id"],
                )
            )
        return self._database

    @contextmanager
    def locked(self):
        self.proxy.ensure_directory()
        lock_path = self.directory / "deployment.lock"
        if lock_path.is_symlink():
            raise DeploymentError("Deployment lock must not be a symlink")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("Another operation owns this deployment") from None
            if self.path.exists():
                private_file(self.path)
                self.state = json.loads(self.path.read_text())
                if self.state.get("config_digest") != self.config_digest:
                    raise DeploymentError(
                        "Configuration changed; reconcile it explicitly before deployment"
                    )
            yield
        finally:
            os.close(descriptor)

    def save(self, phase=None):
        if phase is not None:
            self.state["operation"]["phase"] = phase
        self.state["updated_at"] = datetime.now(UTC).isoformat()
        atomic_json(self.path, self.state)

    def control_diagnostic(self, error, category):
        """Retain bounded private output, never the command line or environment."""
        path = self.directory / f"control-failure-{uuid.uuid4().hex}.json"
        try:
            info = self.directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                return None
            secrets = [
                self.config["database"][key]
                for key in ("owner_password", "web_password", "worker_password")
            ] + [self.config["application"][key] for key in ("secret_key", "worker_secret")]
            variants = set()
            for secret in secrets:
                variants.update(
                    (
                        secret,
                        urllib.parse.quote(secret, safe=""),
                        urllib.parse.quote_plus(secret, safe=""),
                        json.dumps(secret, ensure_ascii=True)[1:-1],
                        json.dumps(secret, ensure_ascii=False)[1:-1],
                        repr(secret)[1:-1],
                    )
                )
            variants = sorted(variants, key=len, reverse=True)
            data = {
                "command": category if category in CONTROL_COMMANDS else "management",
                "created_at": datetime.now(UTC).isoformat(),
                "failure": "timeout"
                if isinstance(error, subprocess.TimeoutExpired)
                else "exit"
                if isinstance(error, subprocess.CalledProcessError)
                else "start",
                "exit_code": getattr(error, "returncode", None),
            }
            for field in ("stdout", "stderr"):
                output = getattr(error, field, None) or b""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                # Redact before truncation so the boundary cannot reveal a secret suffix.
                for secret in variants:
                    output = output.replace(secret, "[REDACTED]")
                encoded = output.encode("utf-8")
                data[field + "_truncated"] = len(encoded) > CONTROL_DIAGNOSTIC_STREAM_BYTES
                data[field] = encoded[-CONTROL_DIAGNOSTIC_STREAM_BYTES:].decode(
                    "utf-8", errors="replace"
                )
            # JSON escaping can expand each output byte; bound the actual file too.
            if len((json.dumps(data, sort_keys=True, indent=2) + "\n").encode()) > (
                CONTROL_DIAGNOSTIC_MAX_BYTES
            ):
                return None
            atomic_json(path, data)
            return path.name
        except OSError:
            # Diagnostic storage must not replace the failure or prevent cleanup/recovery.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def run(self, *arguments, environment=None, timeout=120, input=None, diagnostic=None):
        try:
            return subprocess.run(
                ["docker", *arguments],
                env=dict(os.environ, **(environment or {})),
                input=input,
                check=True,
                capture_output=True,
                timeout=timeout,
            ).stdout
        except (subprocess.SubprocessError, OSError) as error:
            if diagnostic is not None:
                filename = self.control_diagnostic(error, diagnostic)
                location = (
                    f"see private diagnostic {filename} in the state directory"
                    if filename
                    else "private diagnostic could not be written"
                )
                raise DeploymentError(
                    f"A deployment management command failed; {location}", diagnostic=filename
                ) from None
            # Docker inspect/environment and DB failures can contain credentials.
            raise DeploymentError("A deployment container operation failed") from None

    def inspect(self, name):
        result = subprocess.run(
            ["docker", "container", "inspect", name], capture_output=True, timeout=15
        )
        if result.returncode:
            exists = self.run("container", "ls", "--all", "--quiet", "--filter", f"name=^{name}$")
            if exists.strip():
                raise DeploymentError("Cannot inspect existing container")
            return None
        value = json.loads(result.stdout)[0]
        labels = value["Config"].get("Labels") or {}
        if labels.get("org.praximesh.site") != self.state["site_id"]:
            raise DeploymentError("Refusing to control a container owned by another deployment")
        return value

    def networks(self):
        values = {}
        for role, name in self.config["networks"].items():
            network = json.loads(self.run("network", "inspect", name))[0]
            if network["Internal"] != (role == "backend") or network["Driver"] != "bridge":
                raise DeploymentError("Expected one internal backend and one ingress bridge")
            values[role] = network
        pg = json.loads(self.run("container", "inspect", self.config["database"]["container"]))[0]
        if not pg["State"]["Running"] or not set(self.config["networks"].values()) <= set(
            pg["NetworkSettings"]["Networks"]
        ):
            raise DeploymentError(
                "PostgreSQL must be running on both explicitly configured networks"
            )
        return values

    def environment(self, generation, role):
        origin = urllib.parse.urlparse(self.config["proxy"]["origin"])
        gateways = [
            entry["Gateway"]
            for entry in self.networks()["frontend"]["IPAM"]["Config"]
            if entry.get("Gateway")
        ]
        app = self.config["application"]
        return {
            "PRAXIMESH_MODE": "production" if origin.scheme == "https" else "local",
            "APP_SECRET_KEY": app["worker_secret"] if role == "worker" else app["secret_key"],
            "APP_ALLOWED_HOSTS": origin.hostname,
            "CSRF_TRUSTED_ORIGINS": self.config["proxy"]["origin"],
            "TRUST_TLS_PROXY": "1",
            "TRUSTED_PROXY_IPS": ",".join(gateways),
            "DATABASE_URL": self.database.url(generation["database"], role),
            "DB_SSLMODE": "disable",
            "REGISTRATION_OPEN": "1" if app["registration_open"] else "0",
            "JUDGE_ENABLED": "1",
            "JUDGE_BACKEND": "wasi",
            "JUDGE_WASI_SHA256": generation["manifest"]["judge"]["bundle_sha256"],
        }

    def runtime(self, generation, role, name):
        return [
            "--name",
            name,
            "--hostname",
            name,
            "--init",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--memory=" + ("2g" if role == "worker" else "512m"),
            "--pids-limit=" + ("32" if role == "worker" else "64"),
            "--tmpfs=/tmp:rw,noexec,nosuid,size=128m,uid=65532,gid=65532,mode=700",
            "--network",
            self.config["networks"]["frontend" if role == "web" else "backend"],
            "--label",
            "org.praximesh.site=" + self.state["site_id"],
            "--label",
            "org.praximesh.generation=" + generation["id"],
            "--label",
            "org.praximesh.role=" + role,
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=2",
        ]

    def control(self, generation, *command, timeout=120, role="owner"):
        name = f"px-{self.config['instance']}-{generation['id']}-control-{uuid.uuid4().hex[:8]}"
        environment = self.environment(generation, role)
        args = self.runtime(generation, "control", name)
        for key in environment:
            args += ["--env", key]
        try:
            return self.run(
                "run",
                *args,
                generation["manifest"]["images"]["web"],
                "python",
                "manage.py",
                *command,
                environment=environment,
                timeout=timeout,
                diagnostic=command[0] if command else "management",
            )
        finally:
            # A Docker CLI timeout does not terminate the management transaction.
            if self.inspect(name):
                self.run("rm", "--force", "--volumes", name, timeout=20)

    def cleanup_controls(self):
        names = self.run(
            "container",
            "ls",
            "--all",
            "--format",
            "{{.Names}}",
            "--filter",
            "label=org.praximesh.site=" + self.state["site_id"],
            "--filter",
            "label=org.praximesh.role=control",
        )
        for name in names.decode().splitlines():
            if self.inspect(name):
                self.run("rm", "--force", "--volumes", name, timeout=20)

    def service(self, generation, role):
        name = generation[role]
        info = self.inspect(name)
        image = generation["manifest"]["images"][role]
        if info:
            labels = info["Config"]["Labels"]
            if (
                info["Image"] != image
                or labels.get("org.praximesh.generation") != generation["id"]
                or labels.get("org.praximesh.role") != role
            ):
                raise DeploymentError("Existing container identity differs from the journal")
            if not info["State"]["Running"]:
                self.run("start", name)
            return
        environment = self.environment(generation, role)
        args = self.runtime(generation, role, name) + ["--restart=unless-stopped"]
        if role == "web":
            args += ["--publish", "127.0.0.1::8000"]
        for key in environment:
            args += ["--env", key]
        command = (
            ["python", "manage.py", "wasi_service", "--worker-id", name] if role == "worker" else []
        )
        self.run("run", "--detach", *args, image, *command, environment=environment)

    def stop(self, generation):
        if not generation:
            return
        for role in ["web", "worker"]:
            info = self.inspect(generation[role])
            if info and info["State"]["Running"]:
                self.run("stop", "--timeout", "35", generation[role], timeout=45)

    def port(self, generation):
        info = self.inspect(generation["web"])
        if not info or not info["State"]["Running"]:
            raise DeploymentError("Candidate Web is not running")
        ports = info["NetworkSettings"]["Ports"].get("8000/tcp") or []
        if len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
            raise DeploymentError("Application upstream must be loopback-only")
        return int(ports[0]["HostPort"])

    def ready(self, generation):
        name = generation["worker"]
        info = self.inspect(name)
        if not info or not info["State"]["Running"]:
            return False
        started = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00"))
        judge = generation["manifest"]["judge"]
        fingerprint = hashlib.sha256(
            f"wasi:{judge['bundle_sha256']}:{judge['policy_sha256']}".encode()
        ).hexdigest()
        with self.database.connection(generation["database"]) as connection:
            row = connection.execute(
                "SELECT ready, checked_at, fingerprint FROM core_workerhealth WHERE id=%s", (name,)
            ).fetchone()
        return bool(
            row
            and row[0]
            and row[1] >= started
            and (datetime.now(UTC) - row[1]).total_seconds() < 30
            and row[2] == fingerprint
        )

    def wait_ready(self, generation):
        initial = self.inspect(generation["worker"])
        if not initial:
            raise DeploymentError("Candidate worker is missing")
        restart_count = initial["RestartCount"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.ready(generation):
                return
            info = self.inspect(generation["worker"])
            if (
                not info
                or info["RestartCount"] != restart_count
                or info["State"]["Status"] in {"exited", "dead"}
            ):
                raise DeploymentError("Candidate worker failed its fresh startup checks")
            time.sleep(0.5)
        raise DeploymentError("Candidate worker readiness timed out")

    def check(self, generation, token):
        self.wait_ready(generation)
        args = (
            ["check", "--deploy", "--fail-level", "WARNING"]
            if self.config["proxy"]["origin"].startswith("https:")
            else ["check"]
        )
        self.control(generation, *args)
        self.control(generation, "migrate", "--check", "--noinput")
        self.control(
            generation,
            "deployment_check",
            "--token",
            token,
            "--worker-id",
            generation["worker"],
            timeout=100,
        )
        self.health(generation, external=False)

    def health(self, generation, *, external):
        origin = urllib.parse.urlparse(self.config["proxy"]["origin"])
        headers = {}
        if external:
            url = self.config["proxy"]["origin"] + "/health/"
        else:
            url = f"http://127.0.0.1:{self.port(generation)}/health/"
            headers = {"Host": origin.netloc, "X-Forwarded-Proto": origin.scheme}
        context = ssl.create_default_context(cafile=self.config.get("health_ca"))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=10) as response:
                result = json.load(response)
                if result["status"] != "ok" or (external and result["evaluation"] != "available"):
                    raise DeploymentError("Application health check failed")
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            raise DeploymentError("Application health check failed") from None

    def init(self):
        if self.state:
            raise DeploymentError("Deployment already initialized")
        self.networks()
        self.proxy.prepare()
        self.state = {
            "format": 1,
            "site_id": uuid.uuid4().hex,
            "instance": self.config["instance"],
            "config_digest": self.config_digest,
            "current": None,
            "operation": None,
        }
        self.save()

    def expose(self, generation, token, phase="active"):
        self.control(generation, "judge_admission", "resume", "--token", token)
        if not self.ready(generation):
            raise DeploymentError("Specific candidate worker lost readiness")
        # This durable decision MUST precede even persisting an active proxy file.
        self.state["current"] = generation
        self.state["operation"]["exposure_target"] = generation["id"]
        self.save("exposing")
        self.proxy.activate(self.port(generation))
        self.health(generation, external=True)
        self.save(phase)

    def apply(self, release, digest, initial_content=False):
        if not self.state:
            raise DeploymentError("Run init first")
        previous_operation = self.state.get("operation")
        if previous_operation and previous_operation["phase"] not in {
            "active",
            "rolled_back",
            "failed_initial",
        }:
            raise DeploymentError("An unfinished deployment requires recover")
        manifest = verify(release, digest)
        self.run("image", "load", "--input", str(release / "images.tar"), timeout=900)
        inspect_images(manifest)
        self.proxy.probe()
        self.networks()
        self.cleanup_controls()
        self.database.ensure_roles()
        generation_id = uuid.uuid4().hex[:12]
        prefix = f"px-{self.config['instance']}-{generation_id}"
        candidate = {
            "id": generation_id,
            "database": self.database.name(generation_id),
            "web": prefix + "-web",
            "worker": prefix + "-worker",
            "manifest": manifest,
            "release_sha256": digest,
        }
        old = copy.deepcopy(self.state["current"])
        if old and initial_content:
            raise DeploymentError("Initial content import is only allowed on first installation")
        token = str(uuid.uuid4())
        self.state["operation"] = {
            "id": token,
            "phase": "prepared",
            "previous": old,
            "candidate": candidate,
            "exposure_target": None,
        }
        self.save()
        try:
            if old:
                self.save("draining")
                self.control(
                    old,
                    "judge_admission",
                    "drain",
                    "--token",
                    token,
                    "--timeout",
                    "180",
                    timeout=210,
                )
            self.save("quiescing")
            self.proxy.maintenance()
            self.stop(old)
            if old:
                self.database.freeze(old["database"])
                self.save("copying")
                backup = self.directory / "backups"
                backup.mkdir(mode=0o700, exist_ok=True)
                report = self.database.backup_restore(
                    old["database"], candidate["database"], backup / (generation_id + ".dump")
                )
                self.state["operation"]["backup"] = report
            else:
                self.database.create(candidate["database"])
            self.save("migrating")
            self.control(candidate, "migrate", "--noinput", timeout=300)
            if initial_content:
                self.control(candidate, "content_import")
            self.database.grant(candidate["database"])
            self.database.invalidate_health(candidate["database"])
            self.control(candidate, "judge_admission", "pause", "--token", token)
            self.save("validating")
            self.service(candidate, "web")
            self.service(candidate, "worker")
            self.check(candidate, token)
            self.expose(candidate, token)
        except Exception as error:
            phase = self.state["operation"]["phase"]
            self.state["operation"]["failure"] = {
                "phase": phase,
                "type": type(error).__name__,
                "reason": str(error)
                if isinstance(error, (DeploymentError, DeploymentDatabaseError, ProxyError))
                else "See the owned service logs; raw output is not copied into the journal",
            }
            if self.state["operation"]["exposure_target"]:
                self.save("needs_recovery")
                self.proxy.maintenance()
            elif phase == "draining":
                self.save("drain_failed")  # Reading remains available; no task cancellation.
            else:
                self.save("recovering_previous")
                self.restore_previous()
            raise DeploymentError(
                "Deployment failed; inspect the journal and use recover"
            ) from None

    def restore_previous(self):
        operation = self.state["operation"]
        self.proxy.maintenance()
        self.cleanup_controls()
        self.stop(operation["candidate"])
        previous = operation["previous"]
        if not previous:
            self.save("failed_initial")
            return
        self.stop(previous)
        self.database.freeze(previous["database"])
        self.database.thaw(previous["database"])
        self.database.invalidate_health(previous["database"])
        self.control(previous, "judge_admission", "pause", "--token", operation["id"])
        self.service(previous, "web")
        self.service(previous, "worker")
        self.check(previous, operation["id"])
        self.expose(previous, operation["id"], "rolled_back")

    def recover(self):
        if not self.state or not self.state.get("operation"):
            raise DeploymentError("No interrupted deployment to recover")
        self.cleanup_controls()
        operation = self.state["operation"]
        if operation["exposure_target"]:
            # User writes may have happened even if the proxy reload never replied.
            target = self.state["current"]
            if target["id"] != operation["exposure_target"]:
                raise DeploymentError("Exposure journal identity mismatch")
            self.proxy.maintenance()
            self.save("recovering_current")
            self.database.thaw(target["database"])
            self.service(target, "worker")
            self.wait_ready(target)
            self.control(target, "maintain")
            self.control(
                target,
                "judge_admission",
                "drain",
                "--token",
                operation["id"],
                "--timeout",
                "180",
                timeout=210,
            )
            self.stop(target)
            self.database.freeze(target["database"])
            self.database.thaw(target["database"])
            self.database.invalidate_health(target["database"])
            self.service(target, "web")
            self.service(target, "worker")
            self.check(target, operation["id"])
            self.expose(target, operation["id"])
        else:
            if (
                operation["phase"] in {"prepared", "draining", "drain_failed"}
                and operation["previous"]
            ):
                self.control(
                    operation["previous"],
                    "judge_admission",
                    "drain",
                    "--token",
                    operation["id"],
                    "--timeout",
                    "180",
                    timeout=210,
                )
            self.save("recovering_previous")
            self.restore_previous()

    def maintain(self):
        if (
            self.state
            and self.state.get("current")
            and self.state["operation"]["phase"] in {"active", "rolled_back"}
        ):
            self.control(self.state["current"], "maintain")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ["init", "status", "recover", "serve-proxy", "maintain"]:
        commands.add_parser(action)
    apply = commands.add_parser("apply")
    apply.add_argument("--release", type=Path, required=True)
    apply.add_argument("--sha256", required=True)
    apply.add_argument("--initial-content", action="store_true")
    options = parser.parse_args()
    try:
        deployment = Deployment(configuration(options.config), options.state)
        if options.action == "serve-proxy":
            with deployment.locked():
                if not deployment.state:
                    raise DeploymentError("Run init first")
                command = deployment.proxy.command()
                environment = deployment.proxy.environment()
            os.execvpe(command[0], command, environment)
        with deployment.locked():
            if options.action == "apply":
                with deployment.database.operation_lock():
                    deployment.apply(
                        options.release.resolve(), options.sha256, options.initial_content
                    )
            elif options.action in {"recover", "maintain"}:
                with deployment.database.operation_lock():
                    getattr(deployment, options.action)()
            elif options.action != "status":
                getattr(deployment, options.action)()
            state = deployment.state or {}
            print(
                json.dumps(
                    {
                        "instance": state.get("instance"),
                        "phase": (state.get("operation") or {}).get("phase", "initialized"),
                        "current_generation": (state.get("current") or {}).get("id"),
                        "current_release": (state.get("current") or {}).get("release_sha256"),
                    },
                    indent=2,
                )
            )
    except Exception as error:
        location = ""
        if isinstance(error, DeploymentError) and error.diagnostic:
            location = f" Private diagnostic: {error.diagnostic} in the state directory."
        parser.exit(
            1,
            f"Deployment operation failed ({type(error).__name__}); inspect the private journal."
            f"{location}\n",
        )


if __name__ == "__main__":
    main()
