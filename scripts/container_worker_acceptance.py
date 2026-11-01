"""Actual worker image/Compose/PostgreSQL preflight without any GPU device or query."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import SOCKET, STATE, manifest  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_service import HEX_ID, LocalEngine  # noqa: E402
from judge.container_worker_config import validate_config  # noqa: E402
from judge.container_worker_image import worker_digest  # noqa: E402
from judge.driver_artifacts import library_names  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from scripts.build_container_worker import build_worker  # noqa: E402
from scripts.configure_container_worker import create_json  # noqa: E402
from scripts.deploy_database import worker_privileges  # noqa: E402
from scripts.production_acceptance import POSTGRES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--docker-cli-archive", type=Path)
    parser.add_argument("--management-library-dir", type=Path)
    parser.add_argument(
        "--release", type=Path, help="Use the matching verified GPU release instead of rebuilding."
    )
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if (
        bool(args.release) != bool(args.sha256)
        or (
            args.release is None and not all((args.docker_cli_archive, args.management_library_dir))
        )
        or (
            args.release is not None and any((args.docker_cli_archive, args.management_library_dir))
        )
    ):
        parser.error("Choose either --release/--sha256 or both local build inputs")
    engine = LocalEngine(args.docker_socket)
    engine.verify()
    token = "px-container-worker-" + uuid.uuid4().hex[:12]
    output = ROOT / "test-results" / token
    output.mkdir(mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=token + "-"))
    network, volume, tag = token + "-net", token + "-data", token + "-image"
    admin_network = token + "-admin"
    label = "io.praximesh.worker.acceptance=" + token
    created_network = created_admin_network = created_volume = False
    containers = []
    report = {
        "passed": False,
        "gpu_queried": False,
        "gpu_mapped": False,
        "gpu_executed": False,
        "checks": [],
        "containers": containers,
        "entry_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    private_values = []

    def save():
        (output / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    def redact(data):
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        for private in private_values:
            text = text.replace(private, "[private]")
        return text

    def inspect(identifier):
        return json.loads(engine.run("container", "inspect", identifier).stdout)[0]

    def terminal(identifier, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = inspect(identifier)
            if not value["State"]["Running"]:
                return value
            time.sleep(0.1)
        raise TimeoutError("Original check container remains live; retained for owned cleanup")

    try:
        save()
        print(json.dumps({"result": str(output / "result.json")}), flush=True)
        released = None
        if args.release is not None:
            from scripts.release import inspect_images, verify

            released = verify(args.release, args.sha256)
            if "gpu" not in released or released["gpu"]["contracts"]["worker"] != worker_digest():
                raise ValueError("Use a GPU release matching this acceptance application's sources")
            import_client = temporary / "import-client"
            import_client.mkdir(mode=0o700)
            loaded = subprocess.run(
                [
                    "docker",
                    "--config",
                    str(import_client),
                    "--host",
                    "unix://" + str(engine.path),
                    "image",
                    "load",
                    "--input",
                    str(args.release / "images.tar"),
                ],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                capture_output=True,
                timeout=900,
                check=True,
            )
            (output / "import.log").write_text(redact(loaded.stdout + loaded.stderr))
            inspect_images(released, runner=lambda *a, **k: engine.run(*a))
            image = {
                "schema": 1,
                "image": released["images"]["gpu_worker"],
                "worker_contract": released["gpu"]["contracts"]["worker"],
                "controller_contract": released["gpu"]["contracts"]["controller"],
                "management_manifest": released["gpu"]["management"],
            }
            report["release_sha256"] = args.sha256
        else:
            build = output / "worker"
            build.mkdir(mode=0o700)
            image = build_worker(
                engine, args.docker_cli_archive, args.management_library_dir, build, tag
            )
        report["image"] = image
        save()
        engine.run("network", "create", "--internal", "--label", label, network)
        created_network = True
        engine.run("network", "create", "--label", label, admin_network)
        created_admin_network = True
        engine.run("volume", "create", "--label", label, volume)
        created_volume = True
        password, worker_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        private_values.extend((password, worker_password))
        pg_env = temporary / "postgres.env"
        pg_env.write_text("POSTGRES_DB=praximesh\nPOSTGRES_PASSWORD=" + password + "\n")
        pg_env.chmod(0o600)
        pg = (
            engine.run(
                "container",
                "create",
                "--pull=never",
                "--name",
                token + "-postgres",
                "--label",
                label,
                "--network",
                admin_network,
                "--memory=512m",
                "--pids-limit=128",
                "--env-file",
                str(pg_env),
                "--mount",
                f"type=volume,src={volume},dst=/var/lib/postgresql/data",
                "--publish",
                "127.0.0.1::5432",
                POSTGRES,
            )
            .stdout.decode()
            .strip()
        )
        assert HEX_ID.fullmatch(pg)
        containers.append(pg)
        save()
        engine.run("network", "connect", "--alias", "database", network, pg)
        engine.run("container", "start", pg)
        pg_state = inspect(pg)
        report["postgres_state"] = {
            "running": pg_state["State"]["Running"],
            "ports": pg_state["NetworkSettings"]["Ports"],
        }
        save()
        port = int(engine.run("port", pg, "5432/tcp").stdout.decode().strip().rsplit(":", 1)[1])
        admin = f"postgresql://postgres:{password}@127.0.0.1:{port}/praximesh"
        deadline = time.monotonic() + 40
        while True:
            try:
                with psycopg.connect(admin, sslmode="disable", connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned PostgreSQL did not become available") from None
                time.sleep(0.2)
        migrated = subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"],
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LC_ALL": "C.UTF-8",
                "DATABASE_URL": admin,
                "DB_SSLMODE": "disable",
                "APP_DEBUG": "0",
                "JUDGE_ENABLED": "0",
            },
            capture_output=True,
            timeout=180,
        )
        (output / "migrate.log").write_text(redact(migrated.stdout + migrated.stderr))
        assert migrated.returncode == 0, "Owned schema migration failed"
        with psycopg.connect(admin, sslmode="disable", autocommit=True) as database:
            database.execute(
                sql.SQL(
                    "CREATE ROLE gpu_worker LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Literal(worker_password))
            )
            database.execute("REVOKE ALL ON DATABASE praximesh FROM PUBLIC")
            database.execute("GRANT CONNECT ON DATABASE praximesh TO gpu_worker")
            database.execute("GRANT USAGE ON SCHEMA public TO gpu_worker")
            for table, privileges in worker_privileges(database).items():
                database.execute(
                    sql.SQL("GRANT {} ON {} TO gpu_worker").format(
                        sql.SQL(privileges), sql.Identifier(table)
                    )
                )
        state = temporary / "state"
        state.mkdir(mode=0o700)
        version = image["management_manifest"]["driver_version"]
        # Explicit synthetic task identity. --check never opens or executes it.
        configuration = validate_config(
            {
                "schema": 1,
                "enabled": False,
                "gpu": "GPU-11111111-2222-3333-4444-555555555555",
                "worker_id": "synthetic-container-preflight",
                "task_image": "sha256:" + "c" * 64,
                "driver_manifest": {
                    "schema": 1,
                    "profile": "cuda-ptx-driver-v1",
                    "driver_version": version,
                    "libraries": {
                        name: {"soname": soname, "size": 1, "sha256": "a" * 64}
                        for name, soname in library_names(version).items()
                    },
                },
                "database": {
                    "url": f"postgresql://gpu_worker:{worker_password}@database:5432/praximesh",
                    "sslmode": "disable",
                },
            }
        )
        if released is not None:
            configuration["task_image"] = released["images"]["gpu_task"]
            configuration["driver_manifest"] = released["gpu"]["driver"]
            validate_config(configuration)
        create_json(state / "worker.json", configuration)
        config_bytes = (state / "worker.json").read_bytes()
        client = temporary / "docker-client"
        client.mkdir(mode=0o700)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C.UTF-8",
            "PRAXIMESH_GPU_WORKER_IMAGE": image["image"],
            "PRAXIMESH_GPU_STATE": str(state),
            "PRAXIMESH_DOCKER_SOCKET": str(engine.path),
            "PRAXIMESH_GPU_MINOR": "0",
            "PRAXIMESH_GPU_DB_NETWORK": network,
        }
        compose_base = [
            "docker",
            "--config",
            str(client),
            "--host",
            "unix://" + str(engine.path),
            "compose",
            "--project-name",
            token,
            "-f",
            str(ROOT / "deploy/compose.gpu-worker.yaml"),
        ]
        rendered = subprocess.run(
            [*compose_base, "--profile", "*", "config", "--format", "json"],
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        )
        plan = json.loads(rendered.stdout)
        assert set(plan["services"]) == {"gpu-check", "gpu-worker"}
        assert not plan["services"]["gpu-check"].get("devices")
        assert plan["services"]["gpu-worker"]["profiles"] == ["gpu"]
        assert worker_password.encode() not in rendered.stdout
        (output / "compose.json").write_text(redact(rendered.stdout))

        def run_check(name, *, expected=0, arguments=("--check",), extra=None, control=False):
            overrides = {
                "services": {
                    "gpu-check": {
                        "labels": {"io.praximesh.worker.acceptance": token},
                        **(extra or {}),
                    }
                }
            }
            override = temporary / (name + ".json")
            override.write_text(json.dumps(overrides))
            result = subprocess.run(
                [
                    *compose_base,
                    "-f",
                    str(override),
                    "run",
                    "--detach",
                    "--no-deps",
                    "--name",
                    token + "-" + name,
                    "gpu-check",
                    *arguments,
                ],
                env=environment,
                capture_output=True,
                timeout=60,
            )
            (output / (name + "-create.log")).write_text(redact(result.stdout + result.stderr))
            assert result.returncode == 0, "Could not create the owned Compose check"
            identifier = result.stdout.decode().strip()
            assert HEX_ID.fullmatch(identifier)
            containers.append(identifier)
            save()
            value = terminal(identifier)
            host, config = value["HostConfig"], value["Config"]
            if name == "default-disabled":
                assert config["Cmd"] in (None, [])
            assert not host.get("Devices") and not host.get("DeviceRequests")
            assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"] and config["User"] == "0:0"
            assert (host["NanoCpus"], host["Memory"], host["MemorySwap"], host["PidsLimit"]) == (
                10**9,
                512 * 1024**2,
                512 * 1024**2,
                128,
            )
            destinations = {mount["Destination"] for mount in value["Mounts"]}
            assert "/run/praximesh-host-proc" not in destinations
            assert (str(SOCKET) in destinations) is control
            assert not value["NetworkSettings"].get("Ports")
            assert worker_password not in json.dumps(config)
            logs = engine.run("container", "logs", identifier)
            raw = logs.stdout + logs.stderr
            assert all(secret.encode() not in raw for secret in private_values)
            (output / (name + ".log")).write_text(raw.decode())
            assert value["State"]["ExitCode"] == expected, (
                "Unexpected check exit; see sanitized log"
            )
            if expected == 0:
                assert b"GPU readiness was not checked" in raw
            else:
                message = {
                    "disabled": "stage=disabled error=ValueError",
                    "default-disabled": "stage=disabled error=ValueError",
                    "public-config-rejected": "Container GPU worker stopped: SandboxUnavailable",
                    "column-grant-rejected": "stage=database error=ValueError",
                    "source-rejected": "Container GPU worker stopped: ValueError",
                    "legacy-control-rejected": "stage=controller error=ValueError",
                }[name]
                assert message.encode() in raw, "Rejection did not occur at its expected boundary"
            report["checks"].append(
                {"name": name, "container": identifier, "exit_code": expected, "passed": True}
            )
            save()

        run_check(
            "minimum-grants",
            extra={
                "environment": {
                    "DATABASE_URL": "invalid-inherited-url",
                    "JUDGE_ENABLED": "1",
                    "APP_DEBUG": "1",
                }
            },
        )
        run_check("recreated")
        run_check("disabled", expected=1, arguments=("--once",))
        run_check("default-disabled", expected=1, arguments=(), extra={"command": []})
        (state / "worker.json").chmod(0o644)
        try:
            run_check("public-config-rejected", expected=1)
        finally:
            (state / "worker.json").chmod(0o600)
        with psycopg.connect(admin, sslmode="disable", autocommit=True) as database:
            database.execute("GRANT SELECT (password) ON auth_user TO gpu_worker")
            try:
                run_check("column-grant-rejected", expected=1)
            finally:
                database.execute("REVOKE SELECT (password) ON auth_user FROM gpu_worker")
        altered = temporary / "altered.py"
        altered.write_text("# Deliberately changed application source; never imported.\n")
        run_check(
            "source-rejected",
            expected=1,
            extra={"volumes": [f"{altered}:/opt/praximesh/core/models.py:ro"]},
        )
        run_check("restored")
        assert (state / "worker.json").read_bytes() == config_bytes
        assert list(state.iterdir()) == [state / "worker.json"]
        # The actual production image can validate the old no-device controller
        # containment, but must refuse it before constructing a GPU adapter.
        for name in ("locks", "journal"):
            (state / name).mkdir(mode=0o700)
        create_json(state / "mounts.json", manifest(state, engine))
        with DeviceLock(state / "locks", configuration["gpu"]) as guard:
            with ContainerJournal(state / "journal", guard, engine) as journal:
                original_installation = journal.identity
            lock_inode = (guard.directory / guard.filename).stat().st_ino
        configuration["enabled"] = True
        (state / "worker.json").write_text(json.dumps(configuration))
        run_check(
            "legacy-control-rejected",
            expected=1,
            arguments=("--once",),
            control=True,
            extra={
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(state),
                        "target": str(STATE),
                        "read_only": False,
                        "bind": {"create_host_path": False},
                    },
                    {
                        "type": "bind",
                        "source": str(engine.path),
                        "target": str(SOCKET),
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    },
                ],
            },
        )
        with DeviceLock(state / "locks", configuration["gpu"]) as guard:
            assert (guard.directory / guard.filename).stat().st_ino == lock_inode
            with ContainerJournal(state / "journal", guard, engine) as journal:
                assert journal.identity == original_installation and journal.load() is None
        (state / "worker.json").write_bytes(config_bytes)
        assert not (state / "receipts").exists()
        with psycopg.connect(admin, sslmode="disable") as database:
            assert database.execute("SELECT count(*) FROM core_gpudevice").fetchone() == (0,)
            assert database.execute("SELECT count(*) FROM core_submission").fetchone() == (0,)
        assert worker_digest() == image["worker_contract"]
        report["passed"] = True
    except Exception as error:
        report["error"] = type(error).__name__
        (output / "failure.log").write_text(redact(traceback.format_exc()))
        return 1
    finally:
        cleanup = []
        # A CLI observation failure can happen after Docker created the object.
        # Reconcile only this random project's exact ownership label.
        owned = (
            engine.run(
                "container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "label=" + label
            )
            .stdout.decode()
            .split()
        )
        for identifier in reversed(list(dict.fromkeys([*containers, *owned]))):
            value = inspect(identifier)
            assert value["Config"]["Labels"]["io.praximesh.worker.acceptance"] == token
            assert value["Name"].startswith("/" + token + "-")
            if value["State"]["Running"]:
                engine.run("container", "stop", "--time", "1", identifier)
            engine.run("container", "rm", identifier)
            cleanup.append(identifier)
        if created_volume:
            engine.run("volume", "rm", volume)
        if created_network:
            engine.run("network", "rm", network)
        if created_admin_network:
            engine.run("network", "rm", admin_network)
        if (output / "worker/image-id").exists():
            engine.run("image", "rm", tag)
        shutil.rmtree(temporary)
        assert not engine.run(
            "container", "ls", "--all", "--quiet", "--filter", "label=" + label
        ).stdout.strip()
        assert not engine.run(
            "volume", "ls", "--quiet", "--filter", "label=" + label
        ).stdout.strip()
        assert not engine.run(
            "network", "ls", "--quiet", "--filter", "label=" + label
        ).stdout.strip()
        report["owned_resources_removed"] = not temporary.exists()
        report["removed_containers"] = cleanup
        save()
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "checks": len(report["checks"]),
                    "result": str(output / "result.json"),
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
