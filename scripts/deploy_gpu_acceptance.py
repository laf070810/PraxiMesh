"""Exercise GPU attachment files/locks/crashes against a real rootless daemon.

Uses synthetic, unused device metadata and an initialized coordinator journal
fixture. No GPU query, device mapping, worker, database or proxy is started.
This is not GPU activation or end-to-end deployment acceptance.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import manifest  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from scripts.deploy import Deployment, atomic_json  # noqa: E402
from scripts.deploy_gpu import OWNER  # noqa: E402

INTERRUPT = """import os, signal, sys
sys.path.insert(0, sys.argv[1])
from scripts import deploy, deploy_gpu
site = deploy.Deployment(deploy.configuration(__import__('pathlib').Path(sys.argv[2])), sys.argv[3])
point = sys.argv[4]
save, rename = site.save, deploy_gpu.os.rename
def crash():
    os.kill(os.getpid(), signal.SIGKILL)
def interrupted_save(*args, **kwargs):
    save(*args, **kwargs)
    phase = site.state['gpu_attachment']['phase']
    if point == 'intent' and phase == 'prepared' or point == 'confirmation' and phase == 'attached':
        crash()
def interrupted_rename(*args, **kwargs):
    if point == 'temporary':
        crash()
    result = rename(*args, **kwargs)
    if point == 'owner':
        crash()
    return result
site.save = interrupted_save
deploy_gpu.os.rename = interrupted_rename
with site.locked(attach_gpu=True):
    site.attach_gpu()
raise RuntimeError('Expected interruption did not occur')
"""


def observer():
    return {
        "schema": 2,
        "target": {
            "index": 0,
            "uuid": "GPU-" + str(uuid.uuid4()),
            "model": "synthetic-unused-device",
            "driver": "590.48.01",
            "architecture": [8, 6],
        },
        "minor": 0,
        "library": {
            "schema": 2,
            "profile": "nvidia-inventory-v2",
            "driver_version": "590.48.01",
            "library": {
                "name": "libnvidia-ml.so.590.48.01",
                "soname": "libnvidia-ml.so.1",
                "size": 1,
                "sha256": "a" * 64,
            },
            "utility": {"name": "nvidia-smi", "size": 1, "sha256": "b" * 64},
        },
    }


def prepare(directory, engine, caddy):
    directory.mkdir(mode=0o700)
    root = directory / "gpu"
    root.mkdir(mode=0o700)
    for name in ("locks", "journal"):
        (root / name).mkdir(mode=0o700)
    selected = observer()
    device = selected["target"]["uuid"]
    with DeviceLock(root / "locks", device) as guard:
        with ContainerJournal(root / "journal", guard, engine):
            atomic_json(
                root / "mounts.json",
                manifest(
                    root,
                    engine,
                    host_processes=True,
                    device_observer=selected,
                ),
            )
    coordinator = directory / "site"
    coordinator.mkdir(mode=0o700)
    config = {
        "format": 1,
        "instance": "attachmenttest",
        "database": {
            "container": "unused-attachment-database",
            "owner_user": "unused_owner",
            "owner_password": secrets.token_urlsafe(32),
            "web_password": secrets.token_urlsafe(32),
            "worker_password": secrets.token_urlsafe(32),
        },
        "networks": {"backend": "unused-attachment-backend", "frontend": "unused-attachment-front"},
        "application": {
            "secret_key": secrets.token_urlsafe(48),
            "worker_secret": secrets.token_urlsafe(48),
            "registration_open": False,
        },
        "proxy": {"caddy": str(caddy), "origin": "http://127.0.0.1:8080", "bind": "127.0.0.1"},
    }
    initial = Deployment(config, coordinator)
    initial.state = {
        "format": 1,
        "site_id": uuid.uuid4().hex,
        "instance": config["instance"],
        "config_digest": initial.config_digest,
        "current": None,
        "operation": None,
    }
    initial.save()
    config["gpu"] = {"state": str(root), "docker_socket": str(engine.path), "device": device}
    path = directory / "config.json"
    atomic_json(path, config)
    return {"config": path, "site": coordinator, "gpu": root, "device": device}


def install_docker_audit(directory, real_docker):
    """Delegate only Docker info to the real daemon; record every invocation."""
    binaries = directory / "bin"
    binaries.mkdir(mode=0o700)
    audit = directory / "docker-calls.jsonl"
    wrapper = binaries / "docker"
    wrapper.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
valid = args[-3:] in (['info', '--format', '{{{{.ID}}}}'], ['info', '--format', '{{{{json .}}}}'])
valid = valid and (len(args) == 3 or len(args) == 7 and args[0] == '--config' and args[2] == '--host')
fd = os.open({str(audit)!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
with os.fdopen(fd, 'a') as out:
    out.write(json.dumps({{'allowed': valid, 'operation': args[-3:]}}) + '\\n')
if not valid:
    raise SystemExit(99)
os.execv({str(real_docker)!r}, [{str(real_docker)!r}, *args])
""")
    wrapper.chmod(0o700)
    return binaries, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(mode=0o700)
    result = {
        "passed": False,
        "scope": "original node attachment; real filesystem locks and SIGKILL",
        "synthetic_device_metadata": True,
        "synthetic_initialized_site": True,
        "gpu_acceptance": False,
        "worker_or_database_started": False,
        "checks": [],
        "source": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "scripts/deploy.py",
                "scripts/deploy_gpu.py",
                "scripts/deploy_gpu_acceptance.py",
            )
        },
    }
    temporary = None
    try:
        real_docker = Path(shutil.which("docker")).resolve()
        with tempfile.TemporaryDirectory(prefix="px-attach-") as work:
            temporary = Path(work)
            binaries, audit = install_docker_audit(temporary, real_docker)
            os.environ["PATH"] = str(binaries) + os.pathsep + os.environ["PATH"]
            os.environ["DOCKER_HOST"] = "unix://" + str(args.docker_socket)
            os.environ.pop("DOCKER_CONTEXT", None)
            client = temporary / "client"
            client.mkdir(mode=0o700)
            os.environ["DOCKER_CONFIG"] = str(client)
            engine = LocalEngine(args.docker_socket)
            info = engine.verify()
            result["engine"] = {
                key: info[key] for key in ("ID", "ServerVersion", "CgroupVersion", "CgroupDriver")
            }
            caddy = temporary / "unused-caddy"
            caddy.write_text("#!/bin/sh\nexit 99\n")
            caddy.chmod(0o700)
            interruption = temporary / "interrupt.py"
            interruption.write_text(INTERRUPT)

            def run(case, name, *, success=True, site=None, point=None):
                command = (
                    [
                        sys.executable,
                        str(interruption),
                        str(ROOT),
                        str(case["config"]),
                        str(case["site"]),
                        point,
                    ]
                    if point
                    else [
                        sys.executable,
                        str(ROOT / "scripts/deploy.py"),
                        "--config",
                        str(case["config"]),
                        "--state",
                        str(site or case["site"]),
                        "attach-gpu",
                    ]
                )
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=45)
                (output / (name + ".log")).write_bytes(completed.stdout + completed.stderr)
                expected = -signal.SIGKILL if point else 0 if success else 1
                assert completed.returncode == expected, (name, completed.returncode)
                return completed

            primary = prepare(temporary / "normal", engine, caddy)
            lock = primary["gpu"] / "locks" / (primary["device"] + ".lock")
            lock_identity = (lock.stat().st_dev, lock.stat().st_ino)
            mounts = (primary["gpu"] / "mounts.json").read_bytes()
            run(primary, "initial")
            owner = primary["gpu"] / OWNER
            saved = owner.read_bytes()
            journal = primary["site"] / "deployment.json"
            state = journal.read_bytes()
            run(primary, "idempotent")
            assert owner.read_bytes() == saved and journal.read_bytes() == state
            assert (lock.stat().st_dev, lock.stat().st_ino) == lock_identity
            assert (primary["gpu"] / "mounts.json").read_bytes() == mounts
            assert not (primary["gpu"] / "worker.json").exists()
            result["checks"].append("attach-and-retry-preserve-original-identities")
            with DeviceLock(primary["gpu"] / "locks", primary["device"]):
                run(primary, "locked", success=False)
            assert owner.read_bytes() == saved and journal.read_bytes() == state
            result["checks"].append("cross-process-original-device-lock")
            copied = temporary / "copied-site"
            shutil.copytree(primary["site"], copied)
            run(primary, "copied", site=copied, success=False)
            assert owner.read_bytes() == saved
            result["checks"].append("copied-coordinator-cannot-claim-state")

            for point in ("intent", "temporary", "owner", "confirmation"):
                case = prepare(temporary / point, engine, caddy)
                run(case, point + "-killed", point=point)
                before = json.loads((case["site"] / "deployment.json").read_bytes())[
                    "gpu_attachment"
                ]["binding"]
                run(case, point + "-resumed")
                after = json.loads((case["site"] / "deployment.json").read_bytes())[
                    "gpu_attachment"
                ]
                assert after == {"phase": "attached", "binding": before}
                assert not list(case["gpu"].glob(".deployment-owner-*.tmp"))
                result["checks"].append("sigkill-" + point + "-resume-original-intent")

            for record in ("worker.json", "journal/unknown.active.json"):
                name = record.split("/")[-1]
                case = prepare(temporary / name, engine, caddy)
                atomic_json(case["gpu"] / record, {"synthetic": "must remain untouched"})
                before = (case["site"] / "deployment.json").read_bytes()
                run(case, name + "-refused", success=False)
                assert (case["site"] / "deployment.json").read_bytes() == before
                assert json.loads((case["gpu"] / record).read_bytes()) == {
                    "synthetic": "must remain untouched"
                }
                result["checks"].append("refuse-existing-" + name)
            calls = [json.loads(line) for line in audit.read_text().splitlines()]
            assert calls and all(call["allowed"] for call in calls)
            result["docker_commands"] = calls
        result["owned_private_state_removed"] = not temporary.exists()
        assert result["owned_private_state_removed"]
        result["passed"] = True
    except Exception as error:
        result["error"] = type(error).__name__
        raise
    finally:
        result["owned_private_state_removed"] = temporary is not None and not temporary.exists()
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {key: result[key] for key in ("passed", "checks", "owned_private_state_removed")}
            )
        )


if __name__ == "__main__":
    main()
