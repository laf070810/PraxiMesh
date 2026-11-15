"""Prepare an immutable PostgreSQL Compose installation; never start services.

Explicit --gpu preparation reads the selected idle card's metadata. Normal
service startup, full GPU checks and readiness remain separate Compose actions.
"""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.compose_bootstrap import configuration_digest, validate_spec  # noqa: E402
from core.compose_bootstrap_state import InstallationState, initialize  # noqa: E402
from judge.container_controller import manifest as mount_manifest  # noqa: E402
from judge.container_journal import private  # noqa: E402
from judge.container_preparation import create_json, initialize_state  # noqa: E402
from judge.container_processes import host_pid_security_options  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import GPU_UUID  # noqa: E402
from scripts.configure_container_worker import configure  # noqa: E402
from scripts.release import inspect_images, verify, verify_source  # noqa: E402

COMPATIBLE_FILES = (
    "core/compose_bootstrap.py",
    "core/compose_bootstrap_state.py",
    "core/postgres_selfhost.py",
    "core/selfhost.py",
    "scripts/deploy_database.py",
    "deploy/compose.postgres.yaml",
    "deploy/compose.postgres.ai.yaml",
    "deploy/compose.gpu-site.yaml",
    "deploy/compose.gpu-worker.yaml",
    "compose.ai.yaml",
)


def read_private(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        private(os.fstat(stream.fileno()))
        return stream.read()


def publish(path, data, *, existing=False):
    """Idempotent exact bytes; never replace a changed/missing completed configuration."""
    try:
        old = read_private(path)
    except FileNotFoundError:
        if existing:
            raise ValueError("A completed installation file is missing") from None
    else:
        if old != data:
            raise ValueError("Original installation file changed")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink()


def dotenv(values):
    result = []
    for key, value in sorted(values.items()):
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or type(value) is not str
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("Invalid prepared environment field")
        # Compose double-quoted values decode JSON string escapes; $$ prevents
        # interpolation of literal dollars. This is file syntax, never shell code.
        result.append(key + "=" + json.dumps(value, ensure_ascii=False).replace("$", "$$") + "\n")
    return "".join(result).encode()


def frozen_release(path, digest):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Release must use its original canonical absolute directory")
    release = verify(path, digest)
    for name in COMPATIBLE_FILES:
        if (
            release["source_files"].get(name)
            != hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        ):
            raise ValueError("Frozen release does not implement this Compose preparation protocol")
    contents = verify_source(
        path / "source.tar.gz", release["source_files"], capture=release["source_files"]
    )
    return release, contents


def sources(directory, contents, *, existing):
    if not directory.exists():
        if existing:
            raise ValueError("Original frozen sources are missing")
        directory.mkdir(mode=0o700)
    private(directory.lstat(), directory=True)
    for name, data in contents.items():
        target = directory / name
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o644
                or target.resolve() != target
                or target.read_bytes() != data
            ):
                raise ValueError("Prepared frozen source changed")
            continue
        if existing:
            raise ValueError("Prepared frozen source is missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in target.relative_to(directory).parents:
            if parent != Path("."):
                path = directory / parent
                if path.resolve() != path:
                    raise ValueError("Frozen source parent must not be linked")
                path.chmod(0o755)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o644)
    paths = list(directory.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise ValueError("Frozen sources cannot contain links")
    present = {p.relative_to(directory).as_posix() for p in paths if not p.is_dir()}
    if present != set(contents):
        raise ValueError("Unexpected files appeared in the frozen source directory")


def image_operation(engine, *arguments, timeout=300):
    with tempfile.TemporaryDirectory(prefix="px-compose-client-") as client:
        result = subprocess.run(
            ["docker", "--config", client, "--host", "unix://" + str(engine.path), *arguments],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
            capture_output=True,
            timeout=timeout,
        )
    engine.check_socket()
    if result.returncode:
        raise ValueError("Verified Compose image operation failed")
    return result


def imported_images(engine, release, directory):
    image_operation(engine, "load", "--input", str(directory / "images.tar"))
    inspect_images(
        release,
        runner=lambda *args, **kwargs: image_operation(engine, *args, timeout=30),
    )


def make_installation(options, release, engine, engine_info):
    instance, site = "c" + uuid.uuid4().hex[:12], uuid.uuid4().hex
    database = {
        "instance": instance,
        "site_id": site,
        "container": f"px-{instance}-postgres",
        "owner_user": "postgres",
        **{role + "_password": secrets.token_urlsafe(48) for role in ("owner", "web", "worker")},
    }
    spec = validate_spec(
        {
            "schema": 1,
            "instance": instance,
            "site_id": site,
            "generation": uuid.uuid4().hex[:12],
            "source_id": release["source_id"],
            "release_sha256": options.sha256,
            "initial_content": not options.empty,
            "database": database,
            "application_secret": secrets.token_urlsafe(64),
            "wasi_sha256": release["judge"]["bundle_sha256"],
        }
    )
    return {
        "schema": 1,
        "state": str(options.state),
        "release": str(options.release),
        "release_sha256": options.sha256,
        "socket": str(engine.path),
        "socket_identity": list(engine.identity),
        "engine_id": engine_info["ID"],
        "bind": options.bind,
        "port": options.port,
        "ai": options.ai,
        "gpu": options.gpu,
        "bootstrap": spec,
        "web_secret": secrets.token_urlsafe(64),
        "worker_secret": secrets.token_urlsafe(64),
    }


def application_environment(saved, role):
    spec, database = saved["bootstrap"], saved["bootstrap"]["database"]
    instance = spec["instance"]
    url = (
        f"postgresql://px_{instance}_{role}:{quote(database[role + '_password'], safe='')}"
        f"@{database['container']}:5432/px_{instance}_{spec['generation']}"
    )
    return {
        "PRAXIMESH_MODE": "local",
        "PRAXIMESH_COMPOSE_INSTANCE": instance,
        "PRAXIMESH_COMPOSE_GENERATION": spec["generation"],
        "PRAXIMESH_COMPOSE_POSTGRES": database["container"],
        "DATABASE_URL": url,
        "DB_SSLMODE": "disable",
        "APP_SECRET_KEY": saved[role + "_secret"],
        "APP_ALLOWED_HOSTS": "*",
        "APP_DEBUG": "0",
        "REGISTRATION_OPEN": "1",
        "JUDGE_ENABLED": "1",
        "JUDGE_WASI_SHA256": spec["wasi_sha256"],
        "JUDGE_GPU_DEVICES": (saved["gpu"] or "") if role == "web" else "",
    }


def gpu_configuration(saved, release, engine, directory, *, completed):
    expected = {
        "schema": 1,
        "enabled": True,
        "gpu": saved["gpu"],
        "worker_id": "px-" + saved["bootstrap"]["instance"] + "-gpu",
        "task_image": release["images"]["gpu_task"],
        "driver_manifest": release["gpu"]["driver"],
        "database": {
            "url": application_environment(saved, "worker")["DATABASE_URL"],
            "sslmode": "disable",
        },
    }
    if not directory.exists():
        if completed:
            raise ValueError("Original GPU installation state is missing")
        host_pid_security_options(engine.verify())
        initialize_state(directory, engine, saved["gpu"], release["gpu"]["management"])
    private(directory.lstat(), directory=True)
    mounts = json.loads(read_private(directory / "mounts.json"))
    if mounts != mount_manifest(
        directory, engine, host_processes=True, device_observer=mounts.get("device_observer")
    ):
        raise ValueError("Original GPU mount identity changed")
    try:
        configured = json.loads(read_private(directory / "worker.json"))
    except FileNotFoundError:
        if completed:
            raise ValueError("Original GPU worker configuration is missing") from None
        configured = configure(
            directory,
            expected["worker_id"],
            expected["task_image"],
            expected["driver_manifest"],
            expected["database"],
            enabled=True,
        )
    if configured != expected:
        raise ValueError("Original GPU worker configuration differs")
    return mounts["device_observer"]["minor"]


def prepare(options):
    state = options.state
    if not state.is_absolute() or state.resolve() != state or ":" in str(state):
        raise ValueError("Use a canonical absolute private installation path without colons")
    ipaddress.IPv4Address(options.bind)
    if not 0 <= options.port <= 65535 or options.gpu and not GPU_UUID.fullmatch(options.gpu):
        raise ValueError("Choose a valid port and an explicit full physical GPU UUID")
    release, contents = frozen_release(options.release, options.sha256)
    if options.gpu and "gpu" not in release:
        raise ValueError("GPU preparation requires the verified four-image release")
    engine = LocalEngine(options.docker_socket)
    info = engine.verify()
    if state.exists():
        private(state.lstat(), directory=True)
        saved = json.loads(read_private(state / "installation.json"))
        for key, expected in {
            "schema": 1,
            "state": str(state),
            "release": str(options.release),
            "release_sha256": options.sha256,
            "socket": str(engine.path),
            "socket_identity": list(engine.identity),
            "engine_id": info["ID"],
            "bind": options.bind,
            "port": options.port,
            "ai": options.ai,
            "gpu": options.gpu,
        }.items():
            if saved.get(key) != expected:
                raise ValueError("Prepared installation changed; use its original settings")
        validate_spec(saved["bootstrap"])
        if saved["bootstrap"]["initial_content"] != (not options.empty):
            raise ValueError("Original content initialization choice changed")
    else:
        saved = make_installation(options, release, engine, info)
        state.mkdir(mode=0o700)
        create_json(state / "installation.json", saved)
    descriptor = os.open(state / "prepare.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = (state / "prepared.json").exists()
        if completed:
            recorded = json.loads(read_private(state / "prepared.json"))
            if recorded != {
                "schema": 1,
                "installation": hashlib.sha256(
                    read_private(state / "installation.json")
                ).hexdigest(),
            }:
                raise ValueError("Original preparation completion identity changed")
        imported_images(engine, release, options.release)
        sources(state / "source", contents, existing=completed)
        spec = saved["bootstrap"]
        bootstrap_state = state / "bootstrap"
        if not bootstrap_state.exists():
            if completed:
                raise ValueError("Original bootstrap identity is missing")
            initialize(bootstrap_state, configuration_digest(spec))
        with InstallationState(bootstrap_state, configuration_digest(spec)).locked():
            pass
        files = {
            "postgres.env": dotenv(
                {
                    "POSTGRES_USER": spec["database"]["owner_user"],
                    "POSTGRES_PASSWORD": spec["database"]["owner_password"],
                }
            ),
            "bootstrap.env": dotenv(
                {
                    "PRAXIMESH_COMPOSE_BOOTSTRAP": json.dumps(
                        spec, sort_keys=True, separators=(",", ":")
                    )
                }
            ),
            "web.env": dotenv(application_environment(saved, "web")),
            "worker.env": dotenv(application_environment(saved, "worker")),
        }
        compose_files = [state / "source/deploy/compose.postgres.yaml"]
        compose = {
            "COMPOSE_FILE": str(compose_files[0]),
            "COMPOSE_PROJECT_NAME": "px-" + spec["instance"],
            "PRAXIMESH_COMPOSE_INSTANCE": spec["instance"],
            "PRAXIMESH_COMPOSE_SITE": spec["site_id"],
            "PRAXIMESH_SOURCE_DIRECTORY": str(state / "source"),
            "PRAXIMESH_BOOTSTRAP_STATE": str(bootstrap_state),
            "PRAXIMESH_WEB_IMAGE": release["images"]["web"],
            "PRAXIMESH_WORKER_IMAGE": release["images"]["worker"],
            "PRAXIMESH_BIND": saved["bind"],
            "PRAXIMESH_PORT": str(saved["port"]),
            **{
                "PRAXIMESH_" + role.upper() + "_ENV": str(state / (role + ".env"))
                for role in ("postgres", "bootstrap", "web", "worker")
            },
        }
        if saved["gpu"]:
            minor = gpu_configuration(saved, release, engine, state / "gpu", completed=completed)
            compose_files.append(state / "source/deploy/compose.gpu-site.yaml")
            compose.update(
                COMPOSE_PROFILES="gpu",
                PRAXIMESH_GPU_WORKER_IMAGE=release["images"]["gpu_worker"],
                PRAXIMESH_GPU_STATE=str(state / "gpu"),
                PRAXIMESH_GPU_MINOR=str(minor),
                PRAXIMESH_DOCKER_SOCKET=str(engine.path),
                PRAXIMESH_GPU_DB_NETWORK=f"px-{spec['instance']}-database",
            )
        if saved["ai"]:
            compose_files.append(state / "source/deploy/compose.postgres.ai.yaml")
        compose["COMPOSE_FILE"] = ":".join(map(str, compose_files))
        files["compose.env"] = dotenv(compose)
        for name, data in files.items():
            publish(state / name, data, existing=completed)
        client = state / "docker-client"
        if not client.exists():
            if completed:
                raise ValueError("Original Compose client directory is missing")
            client.mkdir(mode=0o700)
        private(client.lstat(), directory=True)
        if (client / "config.json").exists() or (client / "config.json").is_symlink():
            raise ValueError(
                "Compose client configuration must remain separate from credentials and proxies"
            )
        if not completed:
            create_json(
                state / "prepared.json",
                {
                    "schema": 1,
                    "installation": hashlib.sha256(
                        read_private(state / "installation.json")
                    ).hexdigest(),
                },
            )
        return {
            "prepared": True,
            "state": str(state),
            "gpu_configured": bool(saved["gpu"]),
            "services_started": False,
            "compose_command": [
                "docker",
                "--config",
                str(client),
                "--host",
                "unix://" + str(engine.path),
                "compose",
                "--env-file",
                str(state / "compose.env"),
            ],
        }
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--gpu", help="Explicit authorization to read this idle physical GPU UUID")
    parser.add_argument("--ai", action="store_true")
    parser.add_argument(
        "--empty", action="store_true", help="Create schema without importing the example catalog"
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()
    try:
        print(json.dumps(prepare(options)))
        return 0
    except Exception as error:
        print(
            "Compose preparation failed: " + type(error).__name__ + "; original files retained",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
