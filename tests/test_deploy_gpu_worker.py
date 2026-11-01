"""Original private files/locks with a persistent Docker model; never a GPU run.

The model deliberately implements only fixed inspection/create/start/stop calls.
No image is built/loaded, no container or device is touched, and no readiness is
issued. SIGKILL tests concern coordinator files and modeled daemon outcomes.
"""

import copy
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge.container_controller import SOCKET, STATE
from judge.container_processes import HOST_PROC
from judge.gpu_device_lock import DeviceLock
from judge.nvml_artifacts import manifest_digest
from scripts import deploy_gpu_worker
from scripts.deploy_gpu_worker import WorkerLifecycleError, WorkerRuntime
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import remember, state

ORIGINAL_COMMAND = WorkerRuntime.command


class DockerModel:
    def __init__(self, item, directory):
        self.item, self.path = item, directory / "modeled-daemon.json"
        self.guard = None
        self.save(
            {
                "containers": {},
                "calls": [],
                "serial": 0,
                "check_code": 0,
                "check_wait": False,
                "worker_exits": False,
            }
        )
        release = item.generation["manifest"]
        self.image_config = {
            "User": "0:0",
            "Entrypoint": [
                "/usr/local/bin/python",
                "-I",
                "/opt/praximesh/scripts/container_worker_entry.py",
            ],
            "Cmd": None,
            "WorkingDir": "/opt/praximesh",
            "Env": ["PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED=1"],
            "Labels": {
                "io.praximesh.controller.contract": release["gpu"]["contracts"]["controller"],
                "io.praximesh.worker.contract": release["gpu"]["contracts"]["worker"],
                "io.praximesh.controller.observer": manifest_digest(release["gpu"]["management"]),
            },
        }
        self.network = {
            "Name": item.bound.prepared.config["networks"]["backend"],
            "Id": "d" * 64,
            "Internal": True,
            "Driver": "bridge",
        }

    def read(self):
        return json.loads(self.path.read_text())

    def save(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def change(self, callback):
        value = self.read()
        callback(value)
        self.save(value)

    def snapshot(self, runtime, arguments, serial):
        labels = {
            arguments[i + 1].split("=", 1)[0]: arguments[i + 1].split("=", 1)[1]
            for i, arg in enumerate(arguments[:-1])
            if arg == "--label"
        }
        name = arguments[arguments.index("--name") + 1]
        config = copy.deepcopy(self.image_config)
        config.update(
            Hostname=name,
            StopTimeout=240,
            Cmd=["--check"] if runtime.mode == "check" else None,
            Labels=config["Labels"] | labels,
        )
        mounts = [
            {
                "Type": "bind",
                "Source": str(self.item.root),
                "Destination": str(STATE),
                "RW": runtime.mode == "worker",
                "Propagation": "rprivate",
            }
        ]
        devices = []
        host_mounts = [
            {
                "Type": "bind",
                "Source": str(self.item.root),
                "Target": str(STATE),
                "ReadOnly": runtime.mode == "check",
            }
        ]
        if runtime.mode == "worker":
            mounts += [
                {
                    "Type": "bind",
                    "Source": str(runtime.node.engine.path),
                    "Destination": str(SOCKET),
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": "/proc",
                    "Destination": str(HOST_PROC),
                    "RW": False,
                    "Propagation": "rprivate",
                },
            ]
            host_mounts += [
                {
                    "Type": "bind",
                    "Source": str(runtime.node.engine.path),
                    "Target": str(SOCKET),
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": "/proc",
                    "Target": str(HOST_PROC),
                    "ReadOnly": True,
                    "BindOptions": {"Propagation": "rprivate", "ReadOnlyForceRecursive": True},
                },
            ]
            devices = [
                {"PathOnHost": path, "PathInContainer": path, "CgroupPermissions": "rw"}
                for path in ["/dev/nvidia0", "/dev/nvidiactl"]
            ]
        host = {
            "Runtime": "runc",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": self.network["Id"],
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16777216,mode=1777"},
            "Devices": devices,
            "LogConfig": {
                "Type": "local",
                "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
            },
            "Mounts": host_mounts,
        }
        return {
            "Id": f"{serial:064x}",
            "Name": "/" + name,
            "Image": self.item.generation["manifest"]["images"]["gpu_worker"],
            "Config": config,
            "HostConfig": host,
            "Mounts": mounts,
            "NetworkSettings": {
                "Networks": {self.network["Name"]: {"NetworkID": self.network["Id"]}},
                "Ports": {},
            },
            "State": {
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Pid": 0,
                "Status": "created",
                "StartedAt": "0001-01-01T00:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        }

    def command(self, runtime, *args, **kwargs):
        value = self.read()
        value["calls"].append(list(args))
        self.save(value)
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": self.item.generation["manifest"]["images"]["gpu_worker"],
                        "Architecture": "amd64",
                        "Os": "linux",
                        "Config": self.image_config,
                    }
                ]
            ).encode()
        if args[:2] == ("network", "inspect"):
            return json.dumps([self.network]).encode()
        if args[:2] == ("container", "ls"):
            assert args[2:5] == ("--all", "--quiet", "--no-trunc")
            name = args[-1].removeprefix("name=^/").removesuffix("$")
            return "\n".join(
                info["Id"] for info in value["containers"].values() if info["Name"] == "/" + name
            ).encode()
        if args[:2] == ("container", "inspect"):
            matches = [info for info in value["containers"].values() if info["Id"] == args[2]]
            assert len(matches) == 1
            return json.dumps(matches).encode()
        if args[:2] == ("container", "create"):
            value["serial"] += 1
            info = self.snapshot(runtime, list(args[2:]), value["serial"])
            assert info["Name"] not in value["containers"]
            value["containers"][info["Name"]] = info
            self.save(value)
            return info["Id"].encode()
        if args[:2] == ("container", "start"):
            info = next(info for info in value["containers"].values() if info["Id"] == args[2])
            assert not info["State"]["Running"]
            # The real original lock must be available at the actual start call.
            with DeviceLock(self.item.root / "locks", self.item.bound.prepared.device):
                pass
            value["serial"] += 1
            stamp = f"2026-11-01T12:00:{value['serial']:02d}Z"
            info["State"].update(Running=True, Pid=12345, Status="running", StartedAt=stamp)
            if (runtime.mode == "check" and not value["check_wait"]) or value["worker_exits"]:
                info["State"].update(
                    Running=False,
                    Pid=0,
                    Status="exited",
                    ExitCode=value["check_code"],
                    FinishedAt=stamp,
                )
            elif runtime.mode == "worker":
                self.guard = DeviceLock(
                    self.item.root / "locks", self.item.bound.prepared.device
                ).acquire()
            self.save(value)
            return info["Id"].encode()
        if args[:2] == ("container", "stop"):
            assert args[2:4] == ("--timeout", "240")
            info = next(info for info in value["containers"].values() if info["Id"] == args[4])
            info["State"].update(
                Running=False,
                Pid=0,
                Status="exited",
                FinishedAt=info["State"]["StartedAt"],
                ExitCode=137,
            )
            if self.guard is not None:
                self.guard.close()
                self.guard = None
            self.save(value)
            return info["Id"].encode()
        pytest.fail("Unrecognized operation reached Docker model: " + repr(args))


@pytest.fixture
def lifecycle(publication, monkeypatch, tmp_path):
    item = publication
    item.generation["manifest"]["gpu"]["contracts"]["controller"] = "f" * 64
    remember(item, item.generation)
    item.publish(enabled=True)
    model = DockerModel(item, tmp_path)
    monkeypatch.setattr(
        WorkerRuntime,
        "command",
        lambda runtime, *args, **kwargs: model.command(runtime, *args, **kwargs),
    )
    item.model.pause = True

    def pause(name, token):
        assert name == item.generation["database"] and token == item.token
        if not item.model.pause:
            raise WorkerLifecycleError("Modeled original pause was lost")

    monkeypatch.setattr(item.database, "gpu_worker_pause", pause)

    def act(action, mode="worker"):
        host = item.coordinator()
        with host.locked():
            return host.gpu_worker_runtime(item.generation, action, mode=mode)

    try:
        yield SimpleNamespace(item=item, model=model, act=act)
    finally:
        if model.guard is not None:
            model.guard.close()


def test_preflight_and_worker_use_fixed_separate_contracts_and_original_lock_handoff(lifecycle):
    item, model, act = lifecycle.item, lifecycle.model, lifecycle.act
    check = act("check", "check")
    assert check["State"]["ExitCode"] == 0
    assert not check["HostConfig"]["Devices"] and len(check["Mounts"]) == 1
    assert check["Mounts"][0]["RW"] is False
    created = act("create")
    assert created["Id"] != check["Id"]
    started = act("start")
    assert started["State"]["Running"] is True and model.guard is not None
    # Existing creation is idempotent even while this worker holds its device lock.
    assert act("create")["Id"] == created["Id"]
    assert act("start")["Id"] == created["Id"]
    assert act("stop")["State"]["Running"] is False and model.guard is None
    assert state(item)["gpu_runtime"][item.generation["gpu_worker"]]["phase"] == "stopped"
    calls = model.read()["calls"]
    assert sum(call[:2] == ["container", "create"] for call in calls) == 2
    assert sum(call[:2] == ["container", "start"] for call in calls) == 2
    assert item.database.config["worker_password"] not in json.dumps(calls)
    assert item.database.config["worker_password"] not in json.dumps(state(item))
    assert not any(call[:2] == ["container", "rm"] for call in calls)
    item.publish(enabled=True)  # Terminal check/worker records permit an unchanged pair.


def test_worker_cannot_start_without_completed_fixed_preflight(lifecycle):
    lifecycle.act("create")
    with pytest.raises(WorkerLifecycleError, match="preflight is required"):
        lifecycle.act("start")
    assert not any(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"])


@pytest.mark.parametrize("defect", ["exit-code", "oom", "error", "new-start"])
def test_changed_or_failed_preflight_never_authorizes_worker(lifecycle, defect):
    lifecycle.act("check", "check")
    name = "/" + lifecycle.item.generation["gpu_worker"] + "-check"

    def corrupt(value):
        state = value["containers"][name]["State"]
        if defect == "exit-code":
            state["ExitCode"] = 1
        elif defect == "oom":
            state["OOMKilled"] = True
        elif defect == "error":
            state["Error"] = "modeled runtime failure"
        else:
            state["StartedAt"] = "2026-11-01T12:01:00Z"

    lifecycle.model.change(corrupt)
    lifecycle.act("create")
    with pytest.raises(WorkerLifecycleError, match="preflight"):
        lifecycle.act("start")
    assert (
        len(
            [call for call in lifecycle.model.read()["calls"] if call[:2] == ["container", "start"]]
        )
        == 1
    )


def test_known_container_disappearance_and_same_name_substitution_are_not_recreated(lifecycle):
    created = lifecycle.act("create")
    original = lifecycle.model.read()
    lifecycle.model.change(lambda value: value["containers"].clear())
    with pytest.raises(WorkerLifecycleError, match="missing"):
        lifecycle.act("create")
    assert sum(call[:2] == ["container", "create"] for call in lifecycle.model.read()["calls"]) == 1
    original["containers"][created["Name"]]["Id"] = "b" * 64
    lifecycle.model.save(original)
    with pytest.raises(WorkerLifecycleError, match="creation contract"):
        lifecycle.act("stop")
    assert not any(call[:2] == ["container", "stop"] for call in lifecycle.model.read()["calls"])


@pytest.mark.parametrize(
    "field",
    [
        "environment",
        "network",
        "restart",
        "socket",
        "proc",
        "device",
        "entrypoint",
        "memory",
        "label",
    ],
)
def test_runtime_contract_changes_are_refused_before_any_mutation(lifecycle, field):
    created = lifecycle.act("create")

    def corrupt(value):
        info = value["containers"][created["Name"]]
        if field == "environment":
            info["Config"]["Env"].append("DOCKER_HOST=unknown")
        elif field == "network":
            info["NetworkSettings"]["Networks"]["external"] = {}
        elif field == "restart":
            info["HostConfig"]["RestartPolicy"]["Name"] = "always"
        elif field == "socket":
            info["Mounts"][1]["Source"] = "/unknown/socket"
        elif field == "proc":
            info["HostConfig"]["Mounts"][2]["BindOptions"]["ReadOnlyForceRecursive"] = False
        elif field == "device":
            info["HostConfig"]["Devices"].append(
                {
                    "PathOnHost": "/dev/nvidia1",
                    "PathInContainer": "/dev/nvidia1",
                    "CgroupPermissions": "rw",
                }
            )
        elif field == "entrypoint":
            info["Config"]["Entrypoint"] = ["/bin/sh"]
        elif field == "memory":
            info["HostConfig"]["Memory"] *= 2
        else:
            info["Config"]["Labels"][deploy_gpu_worker.LABEL] = "e" * 32

    lifecycle.model.change(corrupt)
    with pytest.raises(WorkerLifecycleError, match="creation contract"):
        lifecycle.act("stop")
    assert not any(
        call[:2] in (["container", "start"], ["container", "stop"])
        for call in lifecycle.model.read()["calls"]
    )


def test_worker_early_exit_is_failure_and_not_a_readiness_result(lifecycle):
    lifecycle.act("check", "check")
    lifecycle.act("create")
    lifecycle.model.change(lambda value: value.update(worker_exits=True))
    with pytest.raises(WorkerLifecycleError, match="exited during startup"):
        lifecycle.act("start")
    record = state(lifecycle.item)["gpu_runtime"][lifecycle.item.generation["gpu_worker"]]
    assert record["phase"] == "start_failed"
    with pytest.raises(WorkerLifecycleError, match="Finish the original"):
        lifecycle.act("start")
    assert sum(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"]) == 2


def test_preflight_timeout_reuses_same_running_container(lifecycle):
    lifecycle.model.change(lambda value: value.update(check_wait=True))
    lifecycle.act("create", "check")
    for _ in range(2):
        host = lifecycle.item.coordinator()
        with host.locked():
            runtime = WorkerRuntime(host, lifecycle.item.generation, mode="check")
            with pytest.raises(WorkerLifecycleError, match="still running"):
                runtime.check(seconds=0)
    assert sum(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"]) == 1
    lifecycle.act("stop", "check")


def test_stop_preserves_original_task_records_and_physical_reservations(lifecycle):
    lifecycle.act("check", "check")
    lifecycle.act("create")
    lifecycle.act("start")
    marker = (
        lifecycle.item.root / "journal" / (lifecycle.item.bound.prepared.device + ".active.json")
    )
    marker.write_text("synthetic original task must remain")
    marker.chmod(0o600)
    lifecycle.item.model.drain = False  # Unrecovered reservation in the modeled DB.
    lifecycle.act("stop")
    assert marker.read_text() == "synthetic original task must remain"
    assert lifecycle.item.model.drain is False


@pytest.mark.parametrize("action", ["start", "stop"])
def test_unrecorded_startup_on_created_container_is_not_adopted(lifecycle, action):
    created = lifecycle.act("create")
    lifecycle.model.change(
        lambda value: value["containers"][created["Name"]]["State"].update(
            StartedAt="2026-11-01T12:01:00Z", Status="exited"
        )
    )
    with pytest.raises(WorkerLifecycleError, match="outside its recorded"):
        lifecycle.act(action)


@pytest.mark.parametrize(
    "stage",
    [
        "prepared",
        "create-response",
        "created",
        "start_requested",
        "start-response",
        "running",
        "stop_requested",
        "stop-response",
        "stopped",
        "completed",
    ],
)
def test_sigkill_reconciles_original_lifecycle_intent_and_never_duplicates_commands(
    lifecycle, stage
):
    item, act, model = lifecycle.item, lifecycle.act, lifecycle.model
    action, mode = "create", "worker"
    if stage in {
        "start_requested",
        "start-response",
        "running",
        "stop_requested",
        "stop-response",
        "stopped",
    }:
        act("check", "check")
        act("create")
        action = "start"
    if stage in {"stop_requested", "stop-response", "stopped"}:
        act("start")
        # The daemon is a file-backed model, not a persistent worker process.
        model.guard.close()
        model.guard = None
        action = "stop"
    if stage == "completed":
        action, mode = "check", "check"
    pid = os.fork()
    if pid == 0:
        from scripts.deploy import Deployment

        save, command = Deployment.save, WorkerRuntime.command

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            name = item.generation["gpu_worker"] + ("-check" if mode == "check" else "")
            record = host.state.get("gpu_runtime", {}).get(name)
            if record and record["phase"] == stage:
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def commanded(runtime, *args, **kwargs):
            result = command(runtime, *args, **kwargs)
            if len(args) > 1 and stage == args[1] + "-response":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Deployment, "save", saved)
                patch.setattr(WorkerRuntime, "command", commanded)
                act(action, mode)
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 20
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned coordinator child did not reach interruption")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        calls = model.read()["calls"]
        before = {
            name: sum(call[:2] == ["container", name] for call in calls)
            for name in ("create", "start", "stop")
        }
        outcome = act(action, mode)
        calls = model.read()["calls"]
        after = {name: sum(call[:2] == ["container", name] for call in calls) for name in before}
        increments = {"prepared": "create", "start_requested": "start", "stop_requested": "stop"}
        assert after == {
            key: count + (increments.get(stage) == key) for key, count in before.items()
        }
        name = item.generation["gpu_worker"] + ("-check" if mode == "check" else "")
        record = state(item)["gpu_runtime"][name]
        assert record["id"] == outcome["Id"]
        assert (
            record["phase"]
            == {"create": "created", "start": "running", "stop": "stopped", "check": "completed"}[
                action
            ]
        )
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_unconfirmed_stop_cannot_be_bypassed_by_configuration_publication(lifecycle, monkeypatch):
    from scripts.deploy import Deployment

    item, model, act = lifecycle.item, lifecycle.model, lifecycle.act
    act("check", "check")
    act("create")
    act("start")
    original = Deployment.save

    def stop_after_intent(host, *args, **kwargs):
        result = original(host, *args, **kwargs)
        record = host.state.get("gpu_runtime", {}).get(item.generation["gpu_worker"])
        if record and record["phase"] == "stop_requested":
            raise InterruptedError("modeled coordinator interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Deployment, "save", stop_after_intent)
        with pytest.raises(InterruptedError):
            act("stop")
    # The old process then exits; the coordinator must still finish its stop intent.
    model.guard.close()
    model.guard = None
    model.change(
        lambda value: value["containers"]["/" + item.generation["gpu_worker"]]["State"].update(
            Running=False, Pid=0, Status="exited"
        )
    )
    with pytest.raises(ValueError, match="lifecycle before publishing"):
        item.publish(enabled=True)
    act("stop")
    item.publish(enabled=True)


def test_private_command_uses_empty_client_configuration_and_explicit_original_socket(
    lifecycle, monkeypatch
):
    host = lifecycle.item.coordinator()
    observed = {}
    with host.locked():
        runtime = WorkerRuntime(host, lifecycle.item.generation)

        def run(*args, **kwargs):
            observed.update(arguments=args, options=kwargs)
            directory = Path(args[1])
            assert directory.is_dir() and not list(directory.iterdir())
            assert args[:1] == ("--config",) and args[2:4] == (
                "--host",
                "unix://" + str(runtime.node.engine.path),
            )
            assert set(kwargs["environment"]) == {"PATH", "LC_ALL"}
            assert kwargs["timeout"] == 255
            return b"fixed-id"

        monkeypatch.setenv("DOCKER_HOST", "unix:///unknown/daemon")
        monkeypatch.setenv("HTTP_PROXY", "private-proxy-value")
        host.run = run
        assert (
            ORIGINAL_COMMAND(
                runtime, "container", "stop", "--timeout", "240", "a" * 64, timeout=255
            )
            == b"fixed-id"
        )
    assert not Path(observed["arguments"][1]).exists()


@pytest.mark.parametrize("failure", ["exception", "oversized"])
def test_private_command_does_not_expose_raw_engine_output(lifecycle, failure):
    host = lifecycle.item.coordinator()
    with host.locked():
        runtime = WorkerRuntime(host, lifecycle.item.generation)

        def run(*args, **kwargs):
            if failure == "exception":
                raise RuntimeError("synthetic private credential")
            return b"x" * (256 * 1024 + 1)

        host.run = run
        with pytest.raises(WorkerLifecycleError, match="operation failed") as error:
            ORIGINAL_COMMAND(runtime, "container", "inspect", "a" * 64)
        assert "credential" not in str(error.value)


@pytest.mark.parametrize("defect", ["running", "privileged", "environment", "proc-readwrite"])
def test_lost_creation_result_cannot_adopt_an_unsafe_or_started_container(
    lifecycle, monkeypatch, defect
):
    original = WorkerRuntime.command

    def create_then_change(runtime, *args, **kwargs):
        result = original(runtime, *args, **kwargs)
        if args[:2] == ("container", "create"):

            def mutate(value):
                info = value["containers"]["/" + runtime.name]
                if defect == "running":
                    info["State"].update(
                        Running=True, Pid=12345, Status="running", StartedAt="2026-11-01T12:01:00Z"
                    )
                elif defect == "privileged":
                    info["HostConfig"]["Privileged"] = True
                elif defect == "environment":
                    info["Config"]["Env"].append("HTTPS_PROXY=unexpected")
                else:
                    info["Mounts"][2]["RW"] = True

            lifecycle.model.change(mutate)
            raise InterruptedError("modeled lost create response")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(WorkerRuntime, "command", create_then_change)
        with pytest.raises(InterruptedError):
            lifecycle.act("create")
    before = lifecycle.model.read()["containers"]
    with pytest.raises(WorkerLifecycleError):
        lifecycle.act("create")
    assert lifecycle.model.read()["containers"] == before
    record = state(lifecycle.item)["gpu_runtime"][lifecycle.item.generation["gpu_worker"]]
    assert record["id"] is None and record["phase"] == "prepared"
    assert sum(call[:2] == ["container", "create"] for call in lifecycle.model.read()["calls"]) == 1


def test_failed_stop_keeps_same_id_and_original_start_instance_for_retry(lifecycle, monkeypatch):
    lifecycle.act("check", "check")
    lifecycle.act("create")
    live = lifecycle.act("start")
    original = WorkerRuntime.command

    def fail_stop(runtime, *args, **kwargs):
        if args[:2] == ("container", "stop"):
            raise WorkerLifecycleError("modeled observation failure")
        return original(runtime, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(WorkerRuntime, "command", fail_stop)
        with pytest.raises(WorkerLifecycleError, match="observation failure"):
            lifecycle.act("stop")
    record = state(lifecycle.item)["gpu_runtime"][lifecycle.item.generation["gpu_worker"]]
    assert record["phase"] == "stop_requested" and record["id"] == live["Id"]
    assert record["started_at"] == live["State"]["StartedAt"]
    assert lifecycle.model.read()["containers"][live["Name"]]["State"]["Running"] is True
    stopped = lifecycle.act("stop")
    assert stopped["Id"] == live["Id"] and stopped["State"]["Running"] is False


@pytest.mark.parametrize("defect", ["pause", "maintenance", "network"])
def test_original_operation_conditions_are_rechecked_before_start(lifecycle, defect):
    lifecycle.act("check", "check")
    lifecycle.act("create")
    if defect == "pause":
        lifecycle.item.model.pause = False
    elif defect == "maintenance":
        lifecycle.item.model.port = 8080
    else:
        lifecycle.model.network["Id"] = "b" * 64
    with pytest.raises(WorkerLifecycleError):
        lifecycle.act("start")
    assert sum(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"]) == 1


@pytest.mark.parametrize("defect", ["paused", "restarting"])
def test_same_start_instance_with_changed_run_state_is_not_accepted(lifecycle, defect):
    lifecycle.act("check", "check")
    lifecycle.act("create")
    live = lifecycle.act("start")
    lifecycle.model.change(
        lambda value: value["containers"][live["Name"]]["State"].update(
            {"Paused" if defect == "paused" else "Restarting": True}
        )
    )
    with pytest.raises(WorkerLifecycleError, match="running state"):
        lifecycle.act("start")
