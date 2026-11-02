"""Real CPU Docker pipes, exit gating and killed-client recovery; no GPU access."""

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge import container_practice_client as client  # noqa: E402
from judge import container_practice_controller as controller  # noqa: E402
from judge.container_controller import controller_digest  # noqa: E402
from judge.container_practice_host import HostWorkspace  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock, DeviceLockError  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts import local_controller_acceptance as base  # noqa: E402

FIXTURE = "/opt/praximesh/tests/local_gpu_client_fixture.py"
SERVICES = ("praximesh-web-1", "praximesh-worker-1", "praximesh-tutor-1")


@contextmanager
def cpu_fixture():
    with base.cpu_fixture(), patch.object(controller, "ENTRY", FIXTURE):
        yield


def services(engine):
    result = []
    for name in SERVICES:
        inspection = engine.run("container", "inspect", name, check=False)
        if inspection.returncode:
            result.append({"name": name, "present": False})
            continue
        rows = json.loads(inspection.stdout)
        item = rows[0]
        result.append(
            {"id": item["Id"], "image": item["Image"], "start": item["State"]["StartedAt"]}
        )
    return result


def case(path, name):
    base.save(path / "journal/fixture-case.json", {"case": name})


def child(args):
    with cpu_fixture(), HostWorkspace(args.child, LocalEngine(args.docker_socket)) as host:
        client.invoke(host, args.image, "initialize")


def run(args):
    args.out = args.out.absolute()
    assert args.out.resolve() == args.out and not args.out.exists()
    args.out.mkdir(mode=0o700)
    engine = LocalEngine(args.docker_socket)
    engine.verify()
    baseline = services(engine)
    tag = "praximesh-local-client-fixture-" + uuid.uuid4().hex
    result = {
        "passed": False,
        "gpu_queried": False,
        "gpu_mapped": False,
        "gpu_executed": False,
        "controller_contract": controller_digest(),
        "execution_digest": execution_digest(),
        "checks": [],
    }
    paths, original_ids, process = [], set(), None
    try:
        with patch.object(
            base,
            "CONTROLLER_SOURCES",
            (*base.CONTROLLER_SOURCES, "tests/local_gpu_client_fixture.py"),
        ):
            image = base.build(engine, args.docker_cli_archive, args.out, tag)
        result["image"] = image
        with cpu_fixture():
            for name in ("normal", "exit-error", "extra-output", "cancel", "killed-client"):
                path = args.out / ("state-" + name)
                base.prepare(path, engine)
                paths.append(path)
                case(path, "hold" if name in {"cancel", "killed-client"} else name)
                if name == "killed-client":
                    with (args.out / "killed-client.log").open("wb") as log:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--docker-socket",
                                args.docker_socket,
                                "--child",
                                str(path),
                                "--image",
                                image,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                        )
                    deadline = time.monotonic() + 30
                    while not (path / "journal/fixture-started.json").exists():
                        assert process.poll() is None and time.monotonic() < deadline
                        time.sleep(0.1)
                    saved = json.loads((path / "journal/local-controller.json").read_bytes())
                    original_ids.add(saved["id"])
                    process.kill()
                    process.wait(timeout=10)
                    with HostWorkspace(path, engine) as host:
                        try:
                            with DeviceLock(path / "locks", host.device):
                                raise AssertionError(
                                    "Original CPU peer must retain the physical lock"
                                )
                        except DeviceLockError:
                            pass
                        case(path, "normal")
                        outcome = client.invoke(host, image, "recover")
                        assert outcome["status"] == "idle"
                        assert host.record["phase"] == "completed"
                else:
                    with HostWorkspace(path, engine) as host:

                        def tick(name=name, path=path):
                            if host.record and host.record.get("id"):
                                original_ids.add(host.record["id"])
                            if (
                                name == "cancel"
                                and (path / "journal/fixture-started.json").exists()
                            ):
                                raise InterruptedError("Fixed acceptance cancellation")

                        if name == "normal":
                            outcome = client.invoke(host, image, "initialize", on_tick=tick)
                            assert outcome == {
                                "status": "initialized",
                                "formal_result": False,
                                "ready": False,
                            }
                        else:
                            try:
                                client.invoke(host, image, "initialize", on_tick=tick)
                                raise AssertionError("Incomplete invocation returned success")
                            except (SandboxUnavailable, InterruptedError):
                                assert host.record["phase"] == "removed"
                        case(path, "normal")
                        assert client.invoke(host, image, "recover")["status"] == "idle"
                        assert host.record["phase"] == "completed"
                assert not list((path / "journal").glob("local-docker-config-*"))
                result["checks"].append(name)
        result["passed"] = True
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        with cpu_fixture():
            for path in paths:
                record_path = path / "journal/local-controller.json"
                if not record_path.exists():
                    continue
                saved = json.loads(record_path.read_bytes())
                base.save(args.out / (path.name + "-record.json"), saved)
                for archived in (path / "journal").glob("local-controller.*.done.json"):
                    base.save(
                        args.out / (path.name + "-" + archived.name),
                        json.loads(archived.read_bytes()),
                    )
                if saved.get("id"):
                    original_ids.add(saved["id"])
                with HostWorkspace(path, engine) as host:
                    controller.ControllerInvocation(host).remove()
        remaining = (
            engine.run("container", "ls", "--all", "--quiet", "--no-trunc").stdout.decode().split()
        )
        result["containers_removed"] = not original_ids.intersection(remaining)
        assert result["containers_removed"]
        result["client_configs_absent"] = all(
            not list((path / "journal").glob("local-docker-config-*")) for path in paths
        )
        assert result["client_configs_absent"]
        for path in paths:
            shutil.rmtree(path)
        result["states_removed"] = all(not path.exists() for path in paths)
        engine.run("image", "rm", "--no-prune", tag, check=False)
        result["services_unchanged"] = services(engine) == baseline
        result["services"] = baseline
        result["containers"] = sorted(original_ids)
        base.save(args.out / "result.json", result)
        assert result["services_unchanged"]
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--docker-cli-archive", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--image", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child is not None:
        return child(args)
    if args.out is None or args.docker_cli_archive is None:
        parser.error("--out and --docker-cli-archive are required")
    run(args)


if __name__ == "__main__":
    main()
