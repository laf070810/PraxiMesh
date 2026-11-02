"""Outer controller fault transitions with real state/locks and a Docker model."""

import copy
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from judge import container_practice_controller as module
from judge.container_practice_host import RECORD, HostWorkspace
from judge.container_practice_state import OPERATION
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable
from tests.test_container_practice_host import prepared as prepared

IMAGE = "sha256:" + "c" * 64
CREATED = "2026-11-02T01:00:00.123456789Z"
STARTED = "2026-11-02T01:00:01.123456789Z"


def snapshot(host, record):
    mounts = [
        {
            "Type": "bind",
            "Source": str(host.root),
            "Destination": str(module.STATE),
            "RW": True,
            "Propagation": "rprivate",
        },
        {
            "Type": "bind",
            "Source": str(host.engine.path),
            "Destination": str(module.SOCKET),
            "RW": False,
            "Propagation": "rprivate",
        },
        {
            "Type": "bind",
            "Source": "/proc",
            "Destination": str(module.HOST_PROC),
            "RW": False,
            "Propagation": "rprivate",
        },
    ]
    declarations = [
        {"Type": "bind", "Source": m["Source"], "Target": m["Destination"], "ReadOnly": not m["RW"]}
        for m in mounts
    ]
    declarations[-1]["BindOptions"] = {"Propagation": "rprivate", "ReadOnlyForceRecursive": True}
    return {
        "Id": uuid.uuid4().hex + uuid.uuid4().hex,
        "Name": "/" + record["name"],
        "Image": record["image"],
        "Created": CREATED,
        "RestartCount": 0,
        "Config": {
            **copy.deepcopy(record["image_config"]),
            "Hostname": record["name"],
            "Entrypoint": [module.PYTHON],
            "Cmd": record["arguments"][-6:],
            "OpenStdin": True,
            "StdinOnce": True,
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Labels": record["image_config"]["Labels"] | module.labels(record),
        },
        "HostConfig": {
            "Runtime": "runc",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "none",
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
            "OomScoreAdj": 200,
            "OomKillDisable": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": {"/tmp": module.TMPFS},
            "Devices": [
                {"PathOnHost": path, "PathInContainer": path, "CgroupPermissions": "rw"}
                for path in module.devices(host.mounts["device_observer"])
            ],
            "LogConfig": {"Type": "none", "Config": {}},
            "Mounts": declarations,
        },
        "Mounts": mounts,
        "NetworkSettings": {"Networks": {"none": {}}, "Ports": {}},
        "State": {
            "Status": "created",
            "Running": False,
            "Paused": False,
            "Restarting": False,
            "Pid": 0,
            "StartedAt": "0001-01-01T00:00:00Z",
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
        },
    }


@pytest.fixture
def lifecycle(prepared, monkeypatch):
    with HostWorkspace(prepared.root, prepared.engine, initialize=True) as host:
        control = module.ControllerInvocation(host)
        value = SimpleNamespace(
            host=host, control=control, state=None, calls=[], lose=None, prepared=prepared
        )
        image_config = {
            "User": "0:0",
            "WorkingDir": "/opt/praximesh",
            "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
            "Entrypoint": [
                module.PYTHON,
                "-I",
                "/opt/praximesh/scripts/container_controller_fixture.py",
            ],
            "Labels": {
                "io.praximesh.controller.contract": module.controller_digest(),
                "io.praximesh.controller.observer": module.manifest_digest(
                    host.mounts["device_observer"]["library"]
                ),
            },
        }

        def run(*args, **kwargs):
            value.calls.append(args)
            data = b""
            if args[:1] == ("info",):
                return prepared.fake.run(*args, **kwargs)
            if args[:2] == ("image", "inspect"):
                data = json.dumps(
                    [{"Id": IMAGE, "Os": "linux", "Architecture": "amd64", "Config": image_config}]
                ).encode()
            elif args[:2] == ("container", "ls"):
                expected = args[-1]
                if value.state and (
                    expected == "id=" + value.state["Id"]
                    or expected == "name=^" + value.state["Name"] + "$"
                ):
                    data = value.state["Id"].encode()
            elif args[:2] == ("container", "create"):
                assert host.read(RECORD)["phase"] == "prepared"
                assert list(args[2:]) == host.read(RECORD)["arguments"]
                value.state = snapshot(host, host.read(RECORD))
                data = value.state["Id"].encode()
            elif args[:2] == ("container", "inspect"):
                assert args[-1] == value.state["Id"]
                data = json.dumps([value.state]).encode()
            elif args[:2] == ("container", "kill"):
                assert host.read(RECORD)["phase"] == "stop_requested"
                assert args[-1] == value.state["Id"]
                value.state["State"].update(Status="exited", Running=False, Pid=0, ExitCode=137)
            elif args[:2] == ("container", "rm"):
                assert host.read(RECORD)["phase"] == "remove_requested"
                assert args[-1] == value.state["Id"] and module.stopped(value.state)
                value.state = None
            else:
                pytest.fail("Unexpected engine call: " + repr(args))
            if value.lose == args[:2]:
                raise SandboxUnavailable("modeled response lost")
            return SimpleNamespace(stdout=data, returncode=0)

        monkeypatch.setattr(prepared.engine, "run", run)
        value.run, value.image_config = run, image_config
        yield value


def create(value):
    value.control.prepare(IMAGE, "run", time.monotonic() + 60)
    value.control.create()


def start(value):
    create(value)
    original = value.control.request_start()
    assert original == value.state["Id"]
    with value.host.physical():
        pass  # The host returned the physical lock before Docker starts the child.
    value.state["State"].update(Status="running", Running=True, Pid=12345, StartedAt=STARTED)
    value.control.observe()
    return original


def test_fixed_creation_start_binding_and_cleanup_without_physical_lock(lifecycle):
    value = lifecycle
    original = start(value)
    assert value.host.read(RECORD)["started_at"] == STARTED
    assert value.host.read(RECORD)["id"] == original
    with DeviceLock(value.host.root / "locks", value.host.device):
        value.control.remove()
    assert value.host.read(RECORD)["phase"] == "removed"
    assert value.state is None
    assert not (value.host.root / "journal" / OPERATION).exists()
    assert not any(args[:2] == ("container", "start") for args in value.calls)
    with pytest.raises(SandboxUnavailable, match="Recover the previous"):
        value.control.prepare(IMAGE, "run", time.monotonic() + 60)


@pytest.mark.parametrize("point", ["prepare", "create", "start-request", "running", "removed"])
def test_recovery_reopens_frozen_original_without_image_or_source_lookup(
    lifecycle, monkeypatch, point
):
    value = lifecycle
    value.control.prepare(IMAGE, "run", time.monotonic() + 60)
    if point != "prepare":
        value.control.create()
    if point in {"start-request", "running", "removed"}:
        value.control.request_start()
    if point in {"running", "removed"}:
        value.state["State"].update(Status="running", Running=True, Pid=12345, StartedAt=STARTED)
    if point == "removed":
        value.control.remove()
    value.calls.clear()
    monkeypatch.setattr(
        module,
        "controller_digest",
        lambda: pytest.fail("Recovery may not use current image identity"),
    )
    recovered = module.ControllerInvocation(value.host)
    recovered.remove()
    assert recovered.record["phase"] == "removed" and value.state is None
    assert not any(
        args[0] == "image" or args[:2] in {("container", "create"), ("container", "start")}
        for args in value.calls
    )


@pytest.mark.parametrize("operation", ["create", "kill", "rm"])
def test_lost_mutation_response_recovers_exact_original(lifecycle, operation):
    value = lifecycle
    if operation == "create":
        value.control.prepare(IMAGE, "run", time.monotonic() + 60)
        value.lose = ("container", "create")
        action = value.control.create
    else:
        start(value)
        value.lose = ("container", operation)
        action = value.control.remove
    with pytest.raises(SandboxUnavailable, match="response lost"):
        action()
    pending = value.host.read(RECORD)
    assert (
        pending["phase"]
        == {"create": "prepared", "kill": "stop_requested", "rm": "remove_requested"}[operation]
    )
    value.lose = None
    recovered = module.ControllerInvocation(value.host)
    recovered.remove()
    assert recovered.record["phase"] == "removed" and value.state is None


@pytest.mark.parametrize(
    "change",
    [
        "replacement",
        "rename",
        "restart",
        "created",
        "memory",
        "stdin",
        "stdin-once",
        "attach",
        "entry",
        "devices",
        "proc",
        "environment",
        "network",
        "mount",
        "boolean",
        "oom-disable",
        "oom-boolean",
        "oom-score",
    ],
)
def test_changed_original_is_preserved_and_never_killed(lifecycle, change):
    value = lifecycle
    start(value)
    if change == "replacement":
        value.state["Id"] = "e" * 64
    elif change == "rename":
        value.state["Name"] += "-renamed"
    elif change == "restart":
        value.state["State"]["StartedAt"] = "2026-11-02T02:00:01Z"
    elif change == "created":
        value.state["Created"] = "2026-11-02T02:00:00Z"
    elif change == "memory":
        value.state["HostConfig"]["Memory"] += 1
    elif change == "stdin":
        value.state["Config"]["OpenStdin"] = False
    elif change == "stdin-once":
        value.state["Config"]["StdinOnce"] = False
    elif change == "attach":
        value.state["Config"]["AttachStdin"] = False
    elif change == "entry":
        value.state["Config"]["Cmd"][1] = "/unrelated.py"
    elif change == "devices":
        value.state["HostConfig"]["Devices"] = []
    elif change == "proc":
        value.state["HostConfig"]["Mounts"][-1]["BindOptions"]["ReadOnlyForceRecursive"] = False
    elif change == "environment":
        value.state["Config"]["Env"].append("UNRELATED=1")
    elif change == "network":
        value.state["NetworkSettings"]["Networks"]["bridge"] = {}
    elif change == "mount":
        value.state["Mounts"][0]["Source"] += "-replacement"
    elif change == "oom-disable":
        value.state["HostConfig"]["OomKillDisable"] = True
    elif change == "oom-boolean":
        value.state["HostConfig"]["OomKillDisable"] = 0
    elif change == "oom-score":
        value.state["HostConfig"]["OomScoreAdj"] = 0
    else:
        value.state["HostConfig"]["Privileged"] = 0
    value.calls.clear()
    with pytest.raises(SandboxUnavailable):
        value.control.remove()
    assert value.state is not None and value.host.read(RECORD)["phase"] == "running"
    assert not any(args[:2] in {("container", "kill"), ("container", "rm")} for args in value.calls)


def test_unregistered_but_started_container_is_not_adopted(lifecycle):
    value = lifecycle
    value.control.prepare(IMAGE, "run", time.monotonic() + 60)
    value.lose = ("container", "create")
    with pytest.raises(SandboxUnavailable):
        value.control.create()
    value.lose = None
    value.state["State"].update(Status="running", Running=True, Pid=12345, StartedAt=STARTED)
    with pytest.raises(SandboxUnavailable, match="already started"):
        module.ControllerInvocation(value.host).remove()
    assert value.host.read(RECORD)["id"] is None


def test_missing_registered_controller_is_not_assumed_cleaned(lifecycle):
    value = lifecycle
    create(value)
    value.state = None
    with pytest.raises(SandboxUnavailable, match="disappeared"):
        value.control.remove()
    assert value.host.read(RECORD)["phase"] == "created"


def test_renamed_original_after_remove_intent_is_not_assumed_gone(lifecycle, monkeypatch):
    value = lifecycle
    create(value)
    original = value.control.command

    def rename(*args):
        if args[:2] == ("container", "rm"):
            value.state["Name"] += "-renamed"
            raise SandboxUnavailable("modeled rename before removal")
        return original(*args)

    monkeypatch.setattr(value.control, "command", rename)
    with pytest.raises(SandboxUnavailable, match="rename before"):
        value.control.remove()
    assert value.host.read(RECORD)["phase"] == "remove_requested"
    with pytest.raises(SandboxUnavailable, match="renamed"):
        module.ControllerInvocation(value.host).remove()
    assert value.state is not None


def test_invalid_controller_image_is_rejected_before_creation_intent(lifecycle):
    value = lifecycle
    value.image_config["Labels"]["io.praximesh.controller.contract"] = "f" * 64
    with pytest.raises(SandboxUnavailable, match="image differs"):
        value.control.prepare(IMAGE, "run", time.monotonic() + 60)
    assert value.host.read(RECORD) is None
    assert not any(args[0] == "container" for args in value.calls)


@pytest.mark.parametrize("phase", ["prepared", "created"])
def test_reopened_intents_cannot_start_or_replay_old_code(lifecycle, phase):
    value = lifecycle
    value.control.prepare(IMAGE, "run", time.monotonic() + 60)
    if phase == "created":
        value.control.create()
    recovered = module.ControllerInvocation(value.host)
    value.calls.clear()
    with pytest.raises(SandboxUnavailable, match="only be cleaned"):
        (recovered.create if phase == "prepared" else recovered.request_start)()
    assert not any(args[0] == "container" for args in value.calls)
    recovered.remove()
    assert recovered.record["phase"] == "removed"


def test_source_change_between_intent_and_start_prevents_launch(lifecycle, monkeypatch):
    from judge import container_practice_protocol

    value = lifecycle
    create(value)
    monkeypatch.setattr(container_practice_protocol, "controller_digest", lambda: "d" * 64)
    with pytest.raises(SandboxUnavailable, match="sources changed"):
        value.control.request_start()
    assert value.host.read(RECORD)["phase"] == "created"
    value.control.remove()


def test_explicit_recovery_archives_removed_controller_without_marking_inner_success(lifecycle):
    value = lifecycle
    start(value)
    value.control.remove()
    original = copy.deepcopy(value.host.record)
    recovered = module.ControllerInvocation(value.host)
    recovered.prepare(IMAGE, "recover", time.monotonic() + 60)
    archive = value.host.read("local-controller." + original["nonce"] + ".done.json")
    assert archive == original and archive["phase"] == "removed"
    assert recovered.record["hello"]["mode"] == "recover"
    assert recovered.record["nonce"] != original["nonce"]
    assert not (value.host.root / "journal" / OPERATION).exists()


def test_mount_inventory_order_does_not_change_original_container_identity(lifecycle):
    value = lifecycle
    start(value)
    value.state["Mounts"].reverse()
    value.control.remove()
    assert value.host.record["phase"] == "removed"


@pytest.mark.parametrize("disabled", [None, False])
def test_docker_oom_kill_default_normalization_preserves_identity(lifecycle, disabled):
    value = lifecycle
    start(value)
    value.state["HostConfig"]["OomKillDisable"] = disabled
    value.control.remove()
    assert value.host.record["phase"] == "removed"
