"""Supervisor rejection/ownership contracts; real lifecycle lives in acceptance."""

import copy
import json
import os
import socket
import time
import uuid
from types import SimpleNamespace

import pytest

from judge import container_entry, container_service
from judge.container_contract import ENTRY, PROTOCOL, PYTHON, STATE
from judge.container_service import PREFIX, ContainerService, LocalEngine, digest
from judge.gpu_command import CommandFailure
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable

IMAGE = "sha256:" + "a" * 64
ID = "b" * 64


def snapshot(service):
    now = time.monotonic()
    arguments = [str(now + 3), str(now + 20), "c" * 32, "--", "/usr/bin/sleep", "30"]
    return {
        "Id": ID,
        "Name": "/" + service.name,
        "Image": IMAGE,
        "RestartCount": 0,
        "Config": {
            "Labels": {
                PREFIX + "protocol": PROTOCOL,
                PREFIX + "owner": service.owner,
                PREFIX + "command": digest(arguments),
            },
            "Cmd": arguments,
            "Entrypoint": [PYTHON, "-I", ENTRY],
            "User": "65532:65532",
            "WorkingDir": STATE,
            "StopSignal": "SIGKILL",
            "StopTimeout": 5,
            "Env": [],
        },
        "State": {
            "Status": "running",
            "Running": True,
            "OOMKilled": False,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "StartedAt": "2026-10-31T00:00:00Z",
            "ExitCode": 0,
            "Pid": 1234,
        },
        "Mounts": [],
        "HostConfig": {
            "Runtime": "runc",
            "NetworkMode": "none",
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "CgroupnsMode": "private",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 2147483648,
            "MemorySwap": 2147483648,
            "CpuQuota": 200000,
            "CpuPeriod": 100000,
            "NanoCpus": 0,
            "PidsLimit": 512,
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": {STATE: "rw,noexec,nosuid,nodev,size=8388608,mode=0700,uid=65532,gid=65532"},
        },
    }


@pytest.fixture
def service(tmp_path):
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        value = ContainerService(guard, uuid.uuid4(), uuid.uuid4(), engine=None, image=IMAGE)
        value.image_env = []
        yield value


def response(value, returncode=0):
    return SimpleNamespace(stdout=json.dumps(value).encode(), returncode=returncode)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("Config", "User", "0"),
        ("Config", "Entrypoint", ["/bin/sh"]),
        ("Config", "Env", ["DATABASE_URL=private"]),
        ("Config", "StopSignal", "SIGTERM"),
        ("HostConfig", "NetworkMode", "host"),
        ("HostConfig", "PidMode", "host"),
        ("HostConfig", "Privileged", True),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
        ("HostConfig", "SecurityOpt", []),
        ("HostConfig", "Binds", ["/run/user/2031/docker.sock:/var/run/docker.sock"]),
        ("HostConfig", "Devices", [{"PathOnHost": "/dev/nvidia0"}]),
        ("HostConfig", "Runtime", "unknown"),
        ("HostConfig", "DeviceCgroupRules", ["c 195:* rwm"]),
        ("HostConfig", "GroupAdd", ["0"]),
        ("HostConfig", "DeviceRequests", [{"Count": -1}]),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "MemorySwap", -1),
        ("HostConfig", "CpuQuota", 0),
        ("HostConfig", "PidsLimit", 0),
        ("HostConfig", "Init", True),
        ("HostConfig", "Tmpfs", {}),
        ("HostConfig", "RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
        ("HostConfig", "LogConfig", {"Type": "json-file", "Config": {}}),
        ("State", "Paused", True),
        ("State", "Running", False),
        ("State", "Pid", 0),
    ],
)
def test_changed_containment_never_authorizes_stop(service, monkeypatch, section, key, value):
    record = snapshot(service)
    record[section][key] = value
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return response([record])

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(SandboxUnavailable, match="containment"):
        service.stop()
    assert calls == [("container", "inspect", service.name)]


def test_foreign_labels_and_same_name_replacement_are_not_stopped(service, monkeypatch):
    record = snapshot(service)
    monkeypatch.setattr(service, "_run", lambda *args, **kwargs: response([record]))
    service.inspect()
    original = copy.deepcopy(record)
    for mutation in (
        lambda r: r.update(Id="d" * 64),
        lambda r: r["State"].update(StartedAt="2026-10-31T01:00:00Z"),
        lambda r: r["Config"]["Labels"].update({PREFIX + "owner": "foreign"}),
        lambda r: r["Config"]["Cmd"].__setitem__(0, "nan"),
    ):
        record.clear()
        record.update(copy.deepcopy(original))
        mutation(record)
        with pytest.raises(SandboxUnavailable):
            service.stop()
        assert service.container_id == ID


def test_disappeared_id_with_reused_name_is_not_absence(service, monkeypatch):
    service.container_id = ID
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            return SimpleNamespace(returncode=1, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=("d" * 64).encode())

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(SandboxUnavailable, match="replaced"):
        service.stop()
    assert all(args[1] in {"inspect", "ls"} for args in calls)


def test_unknown_daemon_state_does_not_authorize_cleanup(service, monkeypatch):
    def run(*args, **kwargs):
        raise CommandFailure("Daemon unavailable")

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(CommandFailure):
        service.remove()
    assert service.container_id is None


def test_stop_and_remove_use_the_verified_id_and_wait_for_stopped_state(service, monkeypatch):
    record = snapshot(service)
    calls, removed = [], False

    def run(*args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[1] == "kill":
            assert args[-1] == ID
            record["State"].update(Running=False, Pid=0, Status="exited", ExitCode=137)
            return response({})
        if args[1] == "rm":
            assert args[-1] == ID and not record["State"]["Running"]
            removed = True
            return response({})
        if removed:
            return SimpleNamespace(returncode=1 if args[1] == "inspect" else 0, stdout=b"")
        return response([record])

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(SandboxUnavailable, match="Stop"):
        service.remove()
    service.stop()
    service.remove()
    assert sum(args[1] == "kill" for args in calls) == 1
    assert sum(args[1] == "rm" for args in calls) == 1


@pytest.mark.parametrize("image", [None, "python:latest", "repo@sha256:" + "a" * 64, "sha256:bad"])
def test_only_local_immutable_image_ids_are_accepted(service, image):
    with pytest.raises(ValueError):
        ContainerService(
            service.guard, service.job_id, service.reservation_token, engine=None, image=image
        )


def test_held_lock_and_independent_purposes_are_required(service):
    recovery = ContainerService(
        service.guard,
        service.job_id,
        service.reservation_token,
        engine=None,
        image=IMAGE,
        purpose="recovery",
    )
    assert recovery.name != service.name
    assert (
        ContainerService(
            service.guard, service.job_id, service.reservation_token, engine=None, image=IMAGE
        ).name
        == service.name
    )
    service.guard.close()
    with pytest.raises(RuntimeError):
        service.inspect()


def test_local_client_ignores_ambient_context_and_proxy_credentials(tmp_path, monkeypatch):
    path = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        engine = LocalEngine(path)
        monkeypatch.setenv("DOCKER_CONTEXT", "unrelated")
        monkeypatch.setenv("HTTPS_PROXY", "https://private-credentials.invalid")
        monkeypatch.setenv("APP_SECRET_KEY", "private")

        def command(args, **kwargs):
            assert args[:2] == ["docker", "--config"]
            assert os.listdir(args[2]) == []
            assert args[3:5] == ["--host", "unix://" + str(path)]
            assert set(kwargs["env"]) == {"PATH", "LC_ALL"}
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(container_service, "run_command", command)
        engine.run("info")
        path.unlink()
        with socket.socket(socket.AF_UNIX) as replacement:
            replacement.bind(str(path))
            with pytest.raises(SandboxUnavailable, match="changed"):
                engine.run("info")


@pytest.mark.parametrize(
    "field,value",
    [
        (0, "99"),
        (0, "nan"),
        (0, "106"),
        (1, "99"),
        (1, "inf"),
        (1, "281"),
        (2, "invalid"),
        (4, "relative"),
    ],
)
def test_entry_rejects_late_unbounded_or_unidentified_start(field, value):
    args = ["104", "120", "a" * 32, "--", "/usr/bin/sleep", "60"]
    args[field] = value
    with pytest.raises(ValueError):
        container_entry.arguments(args, 100)


def test_entry_never_launches_on_host_or_outside_enforced_containment(monkeypatch):
    def launch(*args, **kwargs):
        raise AssertionError("Unexpected process launch")

    monkeypatch.setattr(container_entry.subprocess, "Popen", launch)
    now = time.monotonic()
    assert container_entry.main([str(now + 3), str(now + 20), "a" * 32, "--", "/bin/false"]) == 70


@pytest.mark.parametrize(
    "key,value",
    [
        ("Labels", None),
        ("Labels", []),
        ("Entrypoint", ["/bin/sh"]),
        ("Env", {"unexpected": "mapping"}),
        ("Env", [1]),
        ("Volumes", {"/private": {}}),
        ("OnBuild", ["RUN /unknown"]),
        ("Healthcheck", {"Test": ["CMD", "/unknown"]}),
    ],
)
def test_unknown_image_contract_does_not_authorize_creation(service, monkeypatch, key, value):
    record = {
        "Id": IMAGE,
        "Os": "linux",
        "Config": {
            "Labels": {PREFIX + "protocol": PROTOCOL},
            "Entrypoint": [PYTHON, "-I", ENTRY],
            "Env": [],
        },
    }
    record["Config"][key] = value
    service.image_env = None
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return response([record])

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(SandboxUnavailable, match="contract"):
        service._verify_image()
    assert calls == [("image", "inspect", IMAGE)]


@pytest.mark.parametrize(
    "key,value",
    [
        ("SecurityOptions", []),
        ("CgroupVersion", "1"),
        ("CgroupDriver", "cgroupfs"),
        ("MemoryLimit", False),
        ("SwapLimit", False),
        ("PidsLimit", False),
        ("CpuCfsQuota", False),
    ],
)
def test_unavailable_rootless_resource_control_is_rejected(key, value):
    data = {
        "SecurityOptions": ["name=rootless"],
        "CgroupVersion": "2",
        "CgroupDriver": "systemd",
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
        "CpuCfsQuota": True,
    }
    data[key] = value
    engine = LocalEngine.__new__(LocalEngine)
    engine.run = lambda *args: response(data)
    with pytest.raises(SandboxUnavailable, match="rootless"):
        engine.verify()
