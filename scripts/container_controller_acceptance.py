"""Actual containerized trusted CPU control, shared locks and restart recovery."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON, WORKLOAD_PROFILE, resource_limits  # noqa: E402
from judge.container_controller import (  # noqa: E402
    CONTROLLER_SOURCES,
    LIMITS,
    SOCKET,
    STATE,
    controller_digest,
    manifest,
)
from judge.container_journal import ContainerJournal, PersistentRuntimeService  # noqa: E402
from judge.container_processes import HOST_PROC  # noqa: E402
from judge.container_runtime_service import contract_digest  # noqa: E402
from judge.container_service import HEX_ID, LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock, DeviceLockError  # noqa: E402
from scripts.build_container_controller import build_controller, stage_cli  # noqa: E402,F401
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_runtime_acceptance import build_image  # noqa: E402
from scripts.container_supervision_acceptance import gone, tree  # noqa: E402


class Control:
    def __init__(
        self,
        engine,
        image,
        path,
        config,
        mode,
        *,
        user="0:0",
        socket_path=None,
        host_processes=False,
        proc_path="/proc",
        proc_readonly=True,
        observation_devices=(),
        local_mode=None,
    ):
        if local_mode is not None and local_mode not in {"initialize", "run", "recover"}:
            raise ValueError("Unknown fixed local controller mode")
        self.engine, self.image = engine, image
        self.name = "px-controller-" + uuid.uuid4().hex
        self.label = "io.praximesh.controller.acceptance=" + self.name
        self.host_processes = host_processes
        self.proc_readonly = proc_readonly
        self.observation_devices = tuple(observation_devices)
        observer_mount = []
        if host_processes:
            observer_mount = [
                "--mount",
                f"type=bind,source={proc_path},target={HOST_PROC},"
                + (
                    "readonly,bind-recursive=readonly,bind-propagation=rprivate"
                    if proc_readonly
                    else "bind-propagation=rprivate"
                ),
            ]
        self.arguments = [
            mode,
            config["device"],
            config["image"],
            config["job"],
            config["reservation"],
        ]
        entrypoint = []
        self.entrypoint = [PYTHON, "-I", "/opt/praximesh/scripts/container_controller_fixture.py"]
        if local_mode is not None:
            from judge.container_practice_protocol import ENTRY

            entrypoint = ["--entrypoint", PYTHON]
            self.entrypoint = [PYTHON]
            self.arguments = [
                "-I",
                ENTRY,
                local_mode,
                config["device"],
                uuid.uuid4().hex,
                str(time.monotonic() + 30),
            ]
        self.container_id = (
            engine.run(
                "container",
                "create",
                "--pull=never",
                "--runtime=runc",
                "--name",
                self.name,
                "--label",
                self.label,
                "--user",
                user,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--network=none",
                "--ipc=private",
                "--cgroupns=private",
                "--pids-limit=128",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=1",
                "--restart=no",
                "--log-driver=local",
                "--log-opt=max-size=1m",
                "--log-opt=max-file=1",
                "--log-opt=compress=false",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                "--mount",
                f"type=bind,source={path},target={STATE}",
                "--mount",
                f"type=bind,source={socket_path or engine.path},target={SOCKET},readonly",
                *observer_mount,
                *(f"--device={device}:{device}:rw" for device in self.observation_devices),
                *entrypoint,
                image,
                *self.arguments,
            )
            .stdout.decode()
            .strip()
        )
        if not HEX_ID.fullmatch(self.container_id):
            raise RuntimeError("Missing control container identity")
        self.original = self.inspect()
        self.expected_mounts = {
            str(STATE): (str(path), True),
            str(SOCKET): (str(socket_path or engine.path), False),
        }
        if host_processes:
            self.expected_mounts[str(HOST_PROC)] = (str(proc_path), not proc_readonly)

    def inspect(self):
        result = self.engine.run("container", "inspect", self.container_id, check=False)
        if result.returncode:
            # An error is not absence proof; require the exact ID be absent in listing too.
            listed = self.engine.run(
                "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"
            )
            if self.container_id not in listed.stdout.decode().splitlines():
                return None
            raise RuntimeError("Cannot inspect original controller")
        value = json.loads(result.stdout)[0]
        assert value["Id"] == self.container_id and value["Image"] == self.image
        assert value["Name"] == "/" + self.name and value["Config"]["Cmd"] == self.arguments
        assert value["Config"]["Entrypoint"] == self.entrypoint
        key, expected = self.label.split("=", 1)
        assert value["Config"]["Labels"][key] == expected
        if hasattr(self, "original"):
            assert value["Created"] == self.original["Created"]
        return value

    def start(self):
        info = self.inspect()
        expected = {
            "Runtime": "runc",
            "Privileged": False,
            "CapDrop": ["ALL"],
            "NetworkMode": "none",
            "Devices": [
                {"PathOnHost": device, "PathInContainer": device, "CgroupPermissions": "rw"}
                for device in self.observation_devices
            ],
            "DeviceRequests": None,
            "ReadonlyRootfs": True,
            "PidMode": "",
            "CgroupnsMode": "private",
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
        }
        assert all(info["HostConfig"][key] == value for key, value in expected.items())
        assert info["Config"]["Labels"]["io.praximesh.controller.contract"] == controller_digest()
        assert len(info["Mounts"]) == (3 if self.host_processes else 2)
        assert {
            mount["Destination"]: (mount["Source"], mount["RW"]) for mount in info["Mounts"]
        } == self.expected_mounts
        if self.host_processes and self.proc_readonly:
            mount = next(m for m in info["HostConfig"]["Mounts"] if m["Target"] == str(HOST_PROC))
            options = mount["BindOptions"]
            assert mount["ReadOnly"] is True and options["Propagation"] == "rprivate"
            assert options["ReadOnlyForceRecursive"] is True
            assert not options.get("NonRecursive") and not options.get("ReadOnlyNonRecursive")
        self.engine.run("container", "start", self.container_id)

    def wait(self, code=0):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            value = self.inspect()
            if not value["State"]["Running"]:
                result = self.engine.run("container", "logs", self.container_id)
                logs = (result.stdout + result.stderr).decode()
                assert value["State"]["ExitCode"] == code, (value["State"], logs)
                return logs
            time.sleep(0.05)
        raise TimeoutError("Original controller did not exit")

    def kill(self):
        if self.inspect()["State"]["Running"]:
            self.engine.run("container", "kill", self.container_id)
        self.wait(137)

    def cleanup(self):
        value = self.inspect()
        if value is not None:
            if value["State"]["Running"]:
                self.engine.run("container", "kill", self.container_id)
            assert not self.inspect()["State"]["Running"]
            self.engine.run("container", "rm", self.container_id)
            assert self.inspect() is None
        return {"container": self.container_id, "removed": True, "kind": "controller"}


def wait_report(control, path, name):
    end = time.monotonic() + 40
    while time.monotonic() < end:
        # A final report is published before exit; observe exit before the file
        # so publication between reads cannot look like a missing result.
        state = control.inspect()["State"]
        report = path / (name + ".json")
        if report.exists():
            assert (
                report.stat().st_uid == os.getuid() and stat.S_IMODE(report.stat().st_mode) == 0o600
            )
            return json.loads(report.read_text())
        if not state["Running"]:
            result = control.engine.run("container", "logs", control.container_id)
            logs = (result.stdout + result.stderr).decode()
            raise RuntimeError("Controller stopped before " + name + ": " + logs)
        time.sleep(0.05)
    raise TimeoutError("Controller report timed out: " + name)


def state(engine, root, image, *, host_processes=False, device_observer=None):
    root.mkdir(mode=0o700)
    for name in ("locks", "journal"):
        (root / name).mkdir(mode=0o700)
    save(
        root / "mounts.json",
        manifest(root, engine, host_processes=host_processes, device_observer=device_observer),
    )
    config = {
        "image": image,
        "device": device_observer["target"]["uuid"]
        if device_observer
        else "GPU-" + str(uuid.uuid4()),
        "job": str(uuid.uuid4()),
        "reservation": str(uuid.uuid4()),
    }
    with DeviceLock(root / "locks", config["device"]) as guard:
        with ContainerJournal(root / "journal", guard, engine) as journal:
            original = journal.identity
            service = PersistentRuntimeService(
                journal, config["job"], config["reservation"], image=image
            )
            config.update({"owner": service.owner, "name": service.name, "installation": original})
    save(root / "config.json", config)
    return config


def same_identity(value, config):
    assert value["process"] == {
        "uid": 0,
        "gid": 0,
        "mapped_operator": [os.getuid(), os.getgid()],
        "limits": LIMITS,
    }
    for key in ("owner", "name", "installation"):
        assert value[key] == config[key]


def task_tree(engine, active):
    info = json.loads(engine.run("container", "inspect", active["container"]).stdout)[0]
    assert info["Mounts"] == [] and info["HostConfig"]["DeviceRequests"] is None
    assert info["HostConfig"]["Devices"] == [] and info["HostConfig"]["NetworkMode"] == "none"
    assert info["Config"]["User"] == "65532:65532"
    return tree(engine, SimpleNamespace(container_id=active["container"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--docker-cli-archive", required=True, type=Path)
    parser.add_argument(
        "--observe-device", help="Only read NVML metadata for this explicitly selected GPU UUID"
    )
    parser.add_argument("--nvml-library-dir", type=Path)
    parser.add_argument(
        "--full-device-inventory",
        action="store_true",
        help="Include the pinned full NVIDIA process query (management only, no CUDA)",
    )
    options = parser.parse_args()
    if bool(options.observe_device) != bool(options.nvml_library_dir):
        parser.error("--observe-device and --nvml-library-dir must be supplied together")
    if options.full_device_inventory and not options.observe_device:
        parser.error("--full-device-inventory requires --observe-device and --nvml-library-dir")
    if options.observe_device:
        from judge.gpu_monitor import identify

        # Refuse an unknown selection before building or creating any container.
        identify(options.observe_device)
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-controller-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "management_observation": bool(options.observe_device),
        "controller_contract": controller_digest(),
        "runtime_contract": contract_digest(),
        "checks": [],
        "cleanup": [],
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                *CONTROLLER_SOURCES,
                "scripts/container_controller_acceptance.py",
                "scripts/container_monitor_acceptance.py",
            )
        },
    }
    result_path = out / "result.json"
    save(result_path, report)
    print(json.dumps({"report": str(result_path)}), flush=True)
    controls, states, tags = [], [], []
    storage = tempfile.TemporaryDirectory(prefix=out.name + "-")
    # Rootless bind mounts use local storage; reports remain on repository NFS.
    report["storage"] = storage.name

    def new_state(name):
        path = Path(storage.name) / name
        config = state(engine, path, task_image)
        states.append((path, config))
        return path, config

    def control(path, config, mode, **kwargs):
        current = Control(engine, controller_image, path, config, mode, **kwargs)
        controls.append(current)
        current.start()
        return current

    def passed(name, details):
        report["checks"].append({"scenario": name, "passed": True, **details})
        save(result_path, report)
        print("PASS " + name, flush=True)

    try:
        for name in ("runtime", "controller"):
            directory = out / name
            directory.mkdir(mode=0o700)
            tag = out.name + "-" + name
            tags.append(tag)
            if name == "runtime":
                task_image = build_image(engine, options.runtime_archive, directory, tag)
                report["task_image"] = task_image
            else:
                controller_image = build_controller(
                    engine,
                    options.docker_cli_archive,
                    directory,
                    tag,
                    nvml_library_dir=options.nvml_library_dir,
                    full_inventory=options.full_device_inventory,
                )
                report["controller_image"] = controller_image
            save(result_path, report)
        for mode in ("initialize", "run", "recover"):
            path, config = new_state("local-entry-" + mode)
            current = control(path, config, "normal", local_mode=mode)
            failure = current.wait(2).strip()
            assert failure == "Local controller unavailable; retain original state for recovery."
            assert not (path / "journal" / "local-practice.json").exists()
            assert not (path / "journal" / (config["device"] + ".active.json")).exists()
            passed("local-entry-rejects-v1-" + mode, {"no_task_created": True, "failure": failure})
        if options.observe_device:
            from scripts.container_monitor_acceptance import observation_checks

            observation_checks(
                engine,
                controller_image,
                task_image,
                options.observe_device,
                out,
                Path(storage.name),
                controls,
                states,
                report,
                passed,
            )
        path, config = new_state("locks")
        held = control(path, config, "hold")
        value = wait_report(held, path, "held")
        same_identity(value, config)
        from judge.container_environment import engine_identity
        from judge.container_readiness import SCOPE as STARTUP_SCOPE
        from judge.gpu_policy import execution_digest
        from judge.gpu_receipt import CHECKS as STARTUP_CHECKS

        assert value["execution_digest"] == execution_digest()
        assert value["startup_module"] == {"scope": STARTUP_SCOPE, "checks": list(STARTUP_CHECKS)}
        assert value["engine_environment"] == engine_identity(engine.verify())
        try:
            DeviceLock(path / "locks", config["device"]).acquire()
        except DeviceLockError as error:
            assert str(error) == "Another controller owns this GPU"
        else:
            raise AssertionError("Host acquired a container-held lock")
        contender = control(path, config, "hold")
        assert contender.wait(1).strip() == "DeviceLockError"
        held.kill()
        with DeviceLock(path / "locks", config["device"]):
            contender = control(path, config, "hold")
            assert contender.wait(1).strip() == "DeviceLockError"
        passed("shared-lock-and-uid", value)

        for mode in ("wrong-user", "changed-locks", "changed-socket"):
            path, config = new_state(mode)
            kwargs = {}
            fake = None
            if mode == "wrong-user":
                kwargs["user"] = "65532:65532"
            elif mode == "changed-locks":
                (path / "locks").rename(path / "original-locks")
                (path / "locks").mkdir(mode=0o700)
            else:
                fake = socket.socket(socket.AF_UNIX)
                fake_path = Path(storage.name) / "fake.sock"
                fake.bind(str(fake_path))
                kwargs["socket_path"] = fake_path
            try:
                current = control(path, config, "normal", **kwargs)
                failure = current.wait(2 if mode == "wrong-user" else 1).strip()
                if mode == "wrong-user":
                    assert "can't open file" in failure and "Permission denied" in failure
                else:
                    assert failure == "SandboxUnavailable"
                assert not (path / "journal" / (config["device"] + ".active.json")).exists()
                passed(mode, {"failure": failure, "no_task_created": True})
            finally:
                if fake is not None:
                    fake.close()
                if mode == "changed-locks":
                    (path / "locks").rmdir()
                    (path / "original-locks").rename(path / "locks")

        for mode in ("container-to-container", "container-to-host", "host-to-container"):
            path, config = new_state(mode)
            if mode == "host-to-container":
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        service = PersistentRuntimeService(
                            journal,
                            config["job"],
                            config["reservation"],
                            image=task_image,
                            resource_profile=WORKLOAD_PROFILE,
                        )
                        status = service.start(
                            [
                                PYTHON,
                                "-I",
                                "/opt/praximesh/container_transport_fixture.py",
                                "deadline",
                            ],
                            deadline=time.monotonic() + 80,
                            on_tick=lambda: None,
                        )
                        with ContainerConnection(
                            service, status, on_tick=lambda: None
                        ) as connection:
                            connection.channel.send(
                                {"event": "probe", "canary": "b" * 64, "padding": "x" * 1024**2}
                            )
                            assert connection.channel.receive()["event"] == "guest-ready"
                            connection.channel.send({"event": "hang"})
                            assert connection.channel.receive() == {"event": "busy"}
                            original_record = journal.load()
                            assert original_record["version"] == 2
                            assert original_record["resource_profile"] == WORKLOAD_PROFILE
                            assert status["limits"] == resource_limits(WORKLOAD_PROFILE)
                            active = {
                                "container": service.container_id,
                                "status": status,
                                "journal_version": original_record["version"],
                                "resource_profile": original_record["resource_profile"],
                            }
                            processes = task_tree(engine, active)
            else:
                current = control(path, config, "orphan")
                active = wait_report(current, path, "active")
                same_identity(active, config)
                processes = task_tree(engine, active)
                current.kill()
                (path / "active.json").unlink()
            assert json.loads(engine.run("container", "inspect", active["container"]).stdout)[0][
                "State"
            ]["Running"]
            detail = {"original": active, "original_processes": processes}
            if mode == "container-to-host":
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        assert journal.identity == config["installation"]
                        detail["recovery"] = journal.recover()
                        assert detail["recovery"]["ready"] is False and journal.load() is None
            else:
                current = control(path, config, "recover-normal")
                fresh = wait_report(current, path, "active")
                same_identity(fresh, config)
                assert fresh["container"] != active["container"]
                assert fresh["status"]["nonce"] != active["status"]["nonce"]
                fresh_processes = task_tree(engine, fresh)
                gone(processes)
                (path / "continue").touch(mode=0o600)
                detail["fresh"] = wait_report(current, path, "finished")
                current.wait()
                gone(fresh_processes)
                report["cleanup"].append(
                    {"container": fresh["container"], "removed": True, "kind": "task"}
                )
            gone(processes)
            report["cleanup"].append(
                {"container": active["container"], "removed": True, "kind": "task"}
            )
            passed(mode, {**detail, "process_tree_gone": True})
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        if hasattr(error, "diagnostics"):
            report["private_diagnostics"] = error.diagnostics[:65536].decode(errors="replace")
        raise
    finally:
        cleanup_errors = []
        for current in controls:
            try:
                report["cleanup"].append(current.cleanup())
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + ": " + str(error))
        for path, config in states:
            try:
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        if journal.load() is not None:
                            result = journal.recover()
                            report["cleanup"].append({**result, "kind": "task"})
                shutil.copytree(path, out / path.name)
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + ": " + str(error))
        report["tag_cleanup"] = []
        for tag in tags:
            removed = engine.run("image", "rm", "--no-prune", tag, check=False).returncode == 0
            report["tag_cleanup"].append({"tag": tag, "removed": removed})
            if report["passed"] and not removed:
                cleanup_errors.append("Test image tag removal failed: " + tag)
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            report["passed"] = False
            # Retain recovery data if anything could still own a container.
            storage._finalizer.detach()
        else:
            storage.cleanup()
        report["storage_removed"] = not Path(storage.name).exists()
        save(result_path, report)
        if cleanup_errors:
            raise RuntimeError("Owned cleanup incomplete; inspect private report")
    if not report["passed"]:
        raise SystemExit(1)
    print(json.dumps({"passed": True, "report": str(result_path)}), flush=True)


if __name__ == "__main__":
    main()
