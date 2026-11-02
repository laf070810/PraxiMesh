"""Real CPU-only Docker lifecycle for the local host controller component.

Uses an explicit test image/entry and synthetic device metadata, with all device
arguments removed in this process. It cannot validate or enable GPU execution.
"""

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge import container_practice_controller as controller  # noqa: E402
from judge.container_controller import CONTROLLER_SOURCES, controller_digest  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_practice_host import HostWorkspace  # noqa: E402
from judge.container_practice_state import inode  # noqa: E402
from judge.container_processes import host_manifest  # noqa: E402
from judge.container_service import IMAGE_ID, LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock, DeviceLockError  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.nvml_artifacts import manifest_digest  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.build_container_controller import stage_cli  # noqa: E402

FIXTURE = "/opt/praximesh/tests/local_controller_fixture.py"
OBSERVER = {
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
}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


@contextmanager
def cpu_fixture():
    # Deliberately confined to the acceptance harness; no production flag or
    # relaxed production validator is added for a no-device execution path.
    with (
        patch.object(controller, "devices", return_value={}),
        patch.object(controller, "ENTRY", FIXTURE),
    ):
        yield


def prepare(path, engine):
    path.mkdir(mode=0o700)
    device = "GPU-" + str(uuid.uuid4())
    with DeviceLock(path / "locks", device) as guard:
        with ContainerJournal(path / "journal", guard, engine) as journal:
            value = {
                "version": 4,
                "operator": [os.getuid(), os.getgid()],
                "root": inode(path.stat()),
                "locks": inode((path / "locks").stat()),
                "journal": inode((path / "journal").stat()),
                "socket": inode(engine.path.stat()),
                "engine": journal.identity["engine"],
                "host_processes": host_manifest(),
                "device_observer": {
                    "schema": 2,
                    "minor": 0,
                    "library": OBSERVER,
                    "target": {
                        "index": 0,
                        "uuid": device,
                        "model": "CPU lifecycle fixture",
                        "driver": OBSERVER["driver_version"],
                        "architecture": [8, 6],
                    },
                },
            }
            save(path / "mounts.json", value)
    with HostWorkspace(path, engine, initialize=True):
        pass


def build(engine, archive, out, tag):
    context = out / "context"
    context.mkdir(mode=0o700)
    stage_cli(archive, context / "docker")
    for name in (*CONTROLLER_SOURCES, "tests/local_controller_fixture.py"):
        target = context / "source" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    dockerfile = (ROOT / "deploy/container-controller.Dockerfile").read_text()
    dockerfile += "\nLABEL io.praximesh.controller.observer=" + manifest_digest(OBSERVER) + "\n"
    (context / "Dockerfile").write_text(dockerfile)
    config = out / "docker-config"
    config.mkdir(mode=0o700)
    with (out / "build.log").open("w") as log:
        subprocess.run(
            [
                "docker",
                "--config",
                str(config),
                "--host",
                "unix://" + str(engine.path),
                "build",
                "--tag",
                tag,
                "--iidfile",
                str(out / "image-id"),
                "--build-arg",
                "CONTROLLER_CONTRACT=" + controller_digest(),
                str(context),
            ],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=600,
        )
    image = (out / "image-id").read_text().strip()
    assert IMAGE_ID.fullmatch(image)
    return image


def line(stream, process, seconds=20):
    deadline = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < deadline:
        if select.select([stream], [], [], 0.1)[0]:
            changed = os.read(stream.fileno(), 1)
            if not changed:
                raise RuntimeError(
                    "Fixed CPU fixture ended before readiness: " + str(process.poll())
                )
            data.extend(changed)
            assert len(data) <= 8192
            if data.endswith(b"\n"):
                return json.loads(data)
    raise TimeoutError("Fixed CPU fixture readiness timed out")


@contextmanager
def attached(control):
    original = control.request_start()
    with tempfile.TemporaryDirectory(
        prefix="px-local-attach-", dir=control.host.root.parent
    ) as config:
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                [
                    "docker",
                    "--config",
                    config,
                    "--host",
                    "unix://" + str(control.host.engine.path),
                    "container",
                    "start",
                    "--attach",
                    "--interactive",
                    original,
                ],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            try:
                ready = line(process.stdout, process)
                assert (
                    ready["event"] == "cpu-fixture-ready"
                    and ready["nonce"] == control.record["nonce"]
                    and ready["marker"] == control.host.marker
                )
                assert ready["environment"]["mapped_operator"] == [os.getuid(), os.getgid()]
                control.observe()
                try:
                    with control.host.physical():
                        raise AssertionError("CPU container must hold the original physical lock")
                except DeviceLockError:
                    pass
                yield process, ready
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)
                stderr.seek(0)
                diagnostic = control.host.root.parent / (
                    "attach-" + control.record["nonce"] + ".log"
                )
                diagnostic.write_bytes(stderr.read(65536))
                diagnostic.chmod(0o600)


def child(args):
    engine = LocalEngine(args.docker_socket)
    with cpu_fixture(), HostWorkspace(args.state, engine) as host:
        control = controller.ControllerInvocation(host)
        control.prepare(args.image, "initialize", time.monotonic() + 120)
        control.create()
        with attached(control):
            print(json.dumps({"event": "client-ready", "id": control.record["id"]}), flush=True)
            time.sleep(120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--docker-cli-archive", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--image", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    assert args.out is not None and args.out.is_absolute() and args.docker_cli_archive is not None
    args.out.mkdir(mode=0o700)
    engine = LocalEngine(args.docker_socket)
    engine.verify()
    tag = "praximesh-local-control-check:" + uuid.uuid4().hex
    ids, checks, paths = [], [], []
    result = {
        "passed": False,
        "gpu_queried": False,
        "gpu_mapped": False,
        "gpu_executed": False,
        "synthetic_device_metadata": True,
        "fixed_cpu_fixture": True,
    }
    try:
        image = build(engine, args.docker_cli_archive, args.out, tag)
        result.update(
            image=image,
            controller_contract=controller_digest(),
            execution_digest=execution_digest(),
        )
        with cpu_fixture():
            for case in ("created", "attached", "renamed", "lost-remove-response", "killed-client"):
                path = args.out / ("state-" + case)
                paths.append(path)
                prepare(path, engine)
                if case == "killed-client":
                    with (args.out / "client.log").open("wb") as stderr:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--child",
                                "--docker-socket",
                                str(engine.path),
                                "--state",
                                str(path),
                                "--image",
                                image,
                            ],
                            stdout=subprocess.PIPE,
                            stderr=stderr,
                        )
                        try:
                            ready = line(process.stdout, process, seconds=30)
                            assert ready["event"] == "client-ready"
                            ids.append(ready["id"])
                            process.kill()
                            assert process.wait(timeout=10) < 0
                            with HostWorkspace(path, engine) as host:
                                recovered = controller.ControllerInvocation(host)
                                assert recovered.present()["State"]["Running"] is True
                                recovered.remove()
                                assert recovered.record["phase"] == "removed"
                        finally:
                            if process.poll() is None:
                                process.kill()
                            process.communicate(timeout=10)
                else:
                    with HostWorkspace(path, engine) as host:
                        control = controller.ControllerInvocation(host)
                        control.prepare(image, "initialize", time.monotonic() + 120)
                        control.create()
                        ids.append(control.record["id"])
                        with ExitStack() as stack:
                            if case != "created":
                                stack.enter_context(attached(control))
                            if case == "renamed":
                                engine.run(
                                    "container",
                                    "rename",
                                    control.record["id"],
                                    control.record["name"] + "-renamed",
                                )
                                try:
                                    control.remove()
                                    raise AssertionError("A renamed original must be retained")
                                except SandboxUnavailable as error:
                                    assert "renamed" in str(error)
                                finally:
                                    engine.run(
                                        "container",
                                        "rename",
                                        control.record["id"],
                                        control.record["name"],
                                    )
                            if case == "lost-remove-response":
                                original = engine.run

                                def lose(*command, original=original, **kwargs):
                                    response = original(*command, **kwargs)
                                    if command[:2] == ("container", "rm"):
                                        raise SandboxUnavailable(
                                            "Fixed response loss after actual removal"
                                        )
                                    return response

                                with patch.object(engine, "run", lose):
                                    try:
                                        control.remove()
                                        raise AssertionError(
                                            "Removal response loss must be reported"
                                        )
                                    except SandboxUnavailable as error:
                                        assert "response loss" in str(error)
                                assert host.record["phase"] == "remove_requested"
                            controller.ControllerInvocation(host).remove()
                            assert host.record["phase"] == "removed"
                        with host.physical():
                            pass
                checks.append(case)
        result.update(checks=checks, containers=ids, passed=True)
    finally:
        result["checks"] = checks
        # Read only our original private records to find a create response that
        # may have been lost. Never remove by name or touch unrelated resources.
        for path in paths:
            saved = path / "journal" / "local-controller.json"
            if saved.exists():
                record = json.loads(saved.read_bytes())
                save(args.out / (path.name + "-record.json"), record)
                original = record.get("id")
                if original and original not in ids:
                    ids.append(original)
                if original is None:
                    candidates = (
                        engine.run(
                            "container",
                            "ls",
                            "--all",
                            "--quiet",
                            "--no-trunc",
                            "--filter",
                            "label=" + controller.LABEL + "nonce=" + record["nonce"],
                        )
                        .stdout.decode()
                        .split()
                    )
                    for candidate in candidates:
                        info = json.loads(engine.run("container", "inspect", candidate).stdout)[0]
                        assert (
                            info["Name"] == "/" + record["name"]
                            and info["Image"] == record["image"]
                        )
                        if candidate not in ids:
                            ids.append(candidate)
        remaining = (
            engine.run("container", "ls", "--all", "--quiet", "--no-trunc").stdout.decode().split()
        )
        for original in ids:
            if original not in remaining:
                continue
            info = json.loads(engine.run("container", "inspect", original).stdout)[0]
            assert info["Image"] == result["image"] and info["Config"]["Cmd"][1] == FIXTURE
            assert info["Config"]["Labels"][controller.LABEL + "scope"] == controller.SCOPE
            save(args.out / ("cleanup-container-" + original[:12] + ".json"), info)
            if info["State"]["Running"]:
                engine.run("container", "kill", "--signal=KILL", original)
            engine.run("container", "rm", original)
        remaining = (
            engine.run("container", "ls", "--all", "--quiet", "--no-trunc").stdout.decode().split()
        )
        result["containers_removed"] = all(original not in remaining for original in ids)
        result["containers"] = ids
        for path in paths:
            shutil.rmtree(path)
        result["state_removed"] = all(not path.exists() for path in paths)
        # A SIGKILL bypasses TemporaryDirectory.__exit__ in the child. Every
        # config lives under this new private acceptance output, never /tmp.
        for config in args.out.glob("px-local-attach-*"):
            assert config.is_dir() and not config.is_symlink()
            shutil.rmtree(config)
        result["client_configs_removed"] = not list(args.out.glob("px-local-attach-*"))
        engine.run("image", "rm", "--no-prune", tag, check=False)
        save(args.out / "result.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
