"""Durable lifecycle of the original local control container, without replay.

This module never sends learner input, executes an old image during recovery, or
clears the inner operation. Protocol/process completion remains the caller's job.
"""

import json
import math
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime

from judge.container_contract import PYTHON
from judge.container_controller import SOCKET, STATE, controller_digest
from judge.container_journal import NONCE
from judge.container_monitor import devices
from judge.container_practice_host import RECORD, HostWorkspace
from judge.container_practice_protocol import ENTRY, MODES, PROTOCOL, hello
from judge.container_practice_state import require
from judge.container_processes import HOST_PROC
from judge.container_service import HEX_ID, IMAGE_ID
from judge.gpu_execution_contract import canonical_digest
from judge.nvml_artifacts import manifest_digest

SCOPE = "praximesh-local-control-container-v1"
LABEL = "io.praximesh.local-controller."
TMPFS = "rw,noexec,nosuid,nodev,size=16777216,mode=1777"
ZERO = {"0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000000Z"}
PHASES = {
    "prepared",
    "created",
    "start_requested",
    "running",
    "stop_requested",
    "remove_requested",
    "removed",
    "completed",
}


def timestamp(value):
    require(
        type(value) is str
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value),
        "Invalid original controller timestamp",
    )
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def stopped(info):
    state = info["State"]
    return (
        state.get("Running") is False
        and state.get("Paused") is False
        and state.get("Restarting") is False
        and type(state.get("Pid")) is int
        and state["Pid"] == 0
        and state.get("Status") in {"created", "exited"}
    )


def fingerprint(info):
    value = {key: info[key] for key in ("Id", "Name", "Image", "Created", "Config", "HostConfig")}
    value["HostConfig"] = deepcopy(value["HostConfig"])
    # Docker clears an unsupported false OOM-disable request to null on start.
    # Both mean OOM killing stays enabled; validate_container rejects True/0.
    if value["HostConfig"].get("OomKillDisable") is None:
        value["HostConfig"]["OomKillDisable"] = False
    # Docker's mount inventory is a set keyed by destination; its presentation
    # order is not a lifecycle change. All entries and fields remain bound.
    value["Mounts"] = sorted(info["Mounts"], key=lambda mount: mount["Destination"])
    return canonical_digest(value)


def labels(record):
    return {
        LABEL + "scope": SCOPE,
        LABEL + "nonce": record["nonce"],
        LABEL + "binding": canonical_digest(record["anchor"]),
    }


def arguments(host, record):
    value = record["hello"]
    result = [
        "--pull=never",
        "--runtime=runc",
        "--name",
        record["name"],
        "--hostname",
        record["name"],
        "--user=0:0",
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
        "--oom-score-adj=200",
        "--restart=no",
        "--log-driver=none",
        "--tmpfs=/tmp:" + TMPFS,
        "--interactive",
        "--entrypoint",
        PYTHON,
    ]
    for key, item in labels(record).items():
        result += ["--label", key + "=" + item]
    result += [
        "--mount",
        f"type=bind,source={host.root},target={STATE}",
        "--mount",
        f"type=bind,source={host.engine.path},target={SOCKET},readonly",
        "--mount",
        f"type=bind,source=/proc,target={HOST_PROC},readonly,bind-recursive=readonly,bind-propagation=rprivate",
    ]
    for path in devices(host.mounts["device_observer"]):
        result.append(f"--device={path}:{path}:rw")
    return result + [
        record["image"],
        "-I",
        ENTRY,
        value["mode"],
        value["device"],
        value["nonce"],
        str(value["deadline"]),
    ]


def validate_image(config, contract, observer):
    require(
        type(config) is dict
        and type(config.get("Labels")) is dict
        and config["Labels"].get("io.praximesh.controller.contract") == contract
        and config["Labels"].get("io.praximesh.controller.observer") == manifest_digest(observer)
        and config.get("Entrypoint")
        == [PYTHON, "-I", "/opt/praximesh/scripts/container_controller_fixture.py"]
        and config.get("Cmd") in (None, [])
        and config.get("User") == "0:0"
        and config.get("WorkingDir") == "/opt/praximesh"
        and type(config.get("Env")) is list
        and all(type(item) is str for item in config["Env"])
        and not any(
            config.get(key)
            for key in ("Volumes", "OnBuild", "Healthcheck", "ExposedPorts", "StopSignal", "Shell")
        ),
        "The immutable controller image differs from its frozen contract",
    )
    return config


def validate_record(host, record):
    if record is None:
        return None
    keys = {
        "schema",
        "scope",
        "anchor",
        "nonce",
        "name",
        "image",
        "image_config",
        "hello",
        "arguments",
        "phase",
        "id",
        "created_at",
        "started_at",
        "runtime_sha256",
    }
    require(type(record) is dict and set(record) == keys, "Unknown local controller record")
    value = record["hello"]
    require(
        type(record["schema"]) is int
        and record["schema"] == 1
        and record["scope"] == SCOPE
        and canonical_digest(record["anchor"]) == canonical_digest(host.anchor)
        and type(record["nonce"]) is str
        and NONCE.fullmatch(record["nonce"])
        and record["name"] == "praximesh-local-control-" + record["nonce"]
        and type(record["image"]) is str
        and IMAGE_ID.fullmatch(record["image"])
        and type(record["phase"]) is str
        and record["phase"] in PHASES
        and type(value) is dict
        and set(value)
        == {
            "event",
            "protocol",
            "mode",
            "device",
            "nonce",
            "deadline",
            "binding",
            "controller_contract",
            "execution_digest",
        }
        and value["event"] == "local-ready"
        and value["protocol"] == PROTOCOL
        and type(value["mode"]) is str
        and value["mode"] in MODES
        and value["device"] == host.device
        and value["nonce"] == record["nonce"]
        and value["binding"] == canonical_digest(host.marker["binding"])
        and type(value["deadline"]) in {int, float}
        and math.isfinite(value["deadline"])
        and value["deadline"] > 0
        and all(
            type(value[key]) is str and HEX_ID.fullmatch(value[key])
            for key in ("controller_contract", "execution_digest")
        )
        and record["arguments"] == arguments(host, record),
        "Original local controller invocation changed",
    )
    validate_image(
        record["image_config"],
        value["controller_contract"],
        host.mounts["device_observer"]["library"],
    )
    registered = record["id"] is not None
    require(
        (
            registered
            and type(record["id"]) is str
            and HEX_ID.fullmatch(record["id"])
            and type(record["runtime_sha256"]) is str
            and HEX_ID.fullmatch(record["runtime_sha256"])
            and record["created_at"] is not None
        )
        or (
            not registered
            and all(record[key] is None for key in ("runtime_sha256", "created_at", "started_at"))
            and record["phase"] in {"prepared", "removed", "completed"}
        ),
        "Incomplete original controller registration",
    )
    if registered:
        require(
            record["phase"] != "prepared" and timestamp(record["created_at"]) not in ZERO,
            "Invalid controller creation identity",
        )
    if record["started_at"] is not None:
        require(
            record["phase"] not in {"prepared", "created"}
            and timestamp(record["started_at"]) not in ZERO,
            "Invalid controller startup identity",
        )
    require(
        record["phase"] != "running" or record["started_at"] is not None,
        "Unbound controller startup",
    )
    return record


def validate_container(host, record, info):
    config, runtime, image = info["Config"], info["HostConfig"], record["image_config"]
    expected_devices = [
        {"PathOnHost": path, "PathInContainer": path, "CgroupPermissions": "rw"}
        for path in devices(host.mounts["device_observer"])
    ]
    required = {
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
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Tmpfs": {"/tmp": TMPFS},
        "Devices": expected_devices,
        "LogConfig": {"Type": "none", "Config": {}},
    }
    mounts = {
        str(STATE): (str(host.root), True),
        str(SOCKET): (str(host.engine.path), False),
        str(HOST_PROC): ("/proc", False),
    }
    require(
        type(info["Id"]) is str
        and HEX_ID.fullmatch(info["Id"])
        and info["Name"] == "/" + record["name"]
        and info["Image"] == record["image"]
        and (record["id"] is None or info["Id"] == record["id"])
        and timestamp(info["Created"]) not in ZERO
        and (record["created_at"] is None or info["Created"] == record["created_at"])
        and config.get("User") == "0:0"
        and config.get("Hostname") == record["name"]
        and config.get("Entrypoint") == [PYTHON]
        and config.get("Cmd") == record["arguments"][-6:]
        and config.get("WorkingDir") == image["WorkingDir"]
        and config.get("Labels") == image["Labels"] | labels(record)
        and config.get("Env") == image["Env"]
        and config.get("OpenStdin") is True
        and config.get("StdinOnce") is True
        and all(config.get(key) is True for key in ("AttachStdin", "AttachStdout", "AttachStderr"))
        and not any(
            config.get(key)
            for key in (
                "Tty",
                "Volumes",
                "Healthcheck",
                "ExposedPorts",
                "StopSignal",
                "StopTimeout",
            )
        )
        and all(
            canonical_digest(runtime.get(key)) == canonical_digest(item)
            for key, item in required.items()
        )
        and not any(
            runtime.get(key)
            for key in (
                "Binds",
                "VolumesFrom",
                "CapAdd",
                "DeviceRequests",
                "DeviceCgroupRules",
                "PidMode",
                "UTSMode",
                "UsernsMode",
                "Links",
                "PortBindings",
                "PublishAllPorts",
                "ExtraHosts",
                "Dns",
                "DnsOptions",
                "DnsSearch",
                "CgroupParent",
                "Sysctls",
                "Init",
                "AutoRemove",
            )
        )
        and len(info["Mounts"]) == len(mounts)
        and {m["Destination"]: (m["Source"], m["RW"]) for m in info["Mounts"]} == mounts
        and all(
            m["Type"] == "bind" and m["Propagation"] == "rprivate" and type(m["RW"]) is bool
            for m in info["Mounts"]
        )
        and set(info["NetworkSettings"].get("Networks", {})) == {"none"}
        and not any(info["NetworkSettings"].get("Ports", {}).values())
        and type(info.get("RestartCount")) is int
        and info["RestartCount"] == 0,
        "Original controller differs from its fixed containment or identity",
    )
    require(
        runtime.get("OomKillDisable") is None or runtime.get("OomKillDisable") is False,
        "Controller OOM killing must remain enabled",
    )
    declared = runtime.get("Mounts", [])
    require(
        len(declared) == 3 and {m.get("Target") for m in declared} == set(mounts),
        "Unknown controller mount declarations",
    )
    for mount in declared:
        source, writable = mounts.get(mount.get("Target"), (None, None))
        require(
            mount.get("Type") == "bind"
            and mount.get("Source") == source
            and mount.get("ReadOnly", False) is (not writable),
            "Controller bind declaration changed",
        )
        options = mount.get("BindOptions", {})
        require(
            not options.get("NonRecursive") and not options.get("ReadOnlyNonRecursive"),
            "Controller bind recursion changed",
        )
        if mount.get("Target") == str(HOST_PROC):
            require(
                options.get("ReadOnlyForceRecursive") is True
                and options.get("Propagation") == "rprivate",
                "Original procfs must stay recursively read-only",
            )
    timestamp(info["State"]["StartedAt"])
    state = info["State"]
    require(
        stopped(info)
        or (
            state.get("Status") == "running"
            and state.get("Running") is True
            and state.get("Paused") is False
            and state.get("Restarting") is False
            and type(state.get("Pid")) is int
            and state["Pid"] > 0
            and state["StartedAt"] not in ZERO
        ),
        "Unknown original controller process state",
    )
    if record["started_at"] is not None:
        require(
            info["State"]["StartedAt"] == record["started_at"], "Original controller was restarted"
        )
    require(
        record["runtime_sha256"] is None or fingerprint(info) == record["runtime_sha256"],
        "Original controller configuration changed",
    )
    return info


class ControllerInvocation:
    def __init__(self, host):
        require(type(host) is HostWorkspace, "Original local host workspace is required")
        self.host, self.record = host, deepcopy(validate_record(host, host.record))
        self.fresh = False

    def command(self, *args):
        self.host.check()
        require(
            canonical_digest(self.host.read(RECORD)) == canonical_digest(self.record),
            "Original controller record changed",
        )
        result = self.host.engine.run(*args)
        self.host.check()
        require(
            canonical_digest(self.host.read(RECORD)) == canonical_digest(self.record),
            "Original controller record changed during command",
        )
        return result.stdout

    def save(self, phase):
        self.record["phase"] = phase
        validate_record(self.host, self.record)
        self.host.write_record(self.record)

    def present(self):
        values = (
            self.command(
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "name=^/" + self.record["name"] + "$",
            )
            .decode()
            .split()
        )
        require(
            len(values) <= 1 and all(HEX_ID.fullmatch(value) for value in values),
            "Ambiguous original controller name",
        )
        if self.record["id"] is not None:
            original = (
                self.command(
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "id=" + self.record["id"],
                )
                .decode()
                .split()
            )
            require(original in ([], [self.record["id"]]), "Original controller ID is ambiguous")
            require(
                not values or values == [self.record["id"]],
                "A replacement controller cannot be adopted",
            )
            require(original == values, "Original controller was renamed or replaced")
        if not values:
            return None
        require(
            self.record["id"] is None or values[0] == self.record["id"],
            "A replacement controller cannot be adopted",
        )
        result = json.loads(self.command("container", "inspect", values[0]))
        require(
            type(result) is list and len(result) == 1 and result[0]["Id"] == values[0],
            "Inconsistent original controller inspection",
        )
        return validate_container(self.host, self.record, result[0])

    def prepare(self, image, mode, deadline):
        require(
            self.record is None
            or self.record["phase"] == "completed"
            or (mode == "recover" and self.record["phase"] == "removed"),
            "Recover the previous local invocation first",
        )
        require(
            type(image) is str and IMAGE_ID.fullmatch(image),
            "An immutable controller image is required",
        )
        with self.host.physical() as state:
            operation = state.operation()
            if mode != "recover":
                require(
                    state.journal.load() is None
                    and (operation is None or operation["phase"] != "pending"),
                    "Recover the original inner operation first",
                )
            nonce = uuid.uuid4().hex
            greeting = hello(
                mode,
                self.host.device,
                nonce,
                deadline,
                canonical_digest(self.host.marker["binding"]),
            )
            images = json.loads(self.command("image", "inspect", image))
            require(
                type(images) is list
                and len(images) == 1
                and images[0]["Id"] == image
                and images[0]["Os"] == "linux"
                and images[0]["Architecture"] == "amd64",
                "Immutable controller image is unavailable",
            )
            config = deepcopy(
                validate_image(
                    images[0]["Config"],
                    controller_digest(),
                    self.host.mounts["device_observer"]["library"],
                )
            )
            if self.record is not None:
                name = "local-controller." + self.record["nonce"] + ".done.json"
                existing = state.journal.read(name)
                require(
                    existing is None or canonical_digest(existing) == canonical_digest(self.record),
                    "Original controller archive changed",
                )
                if existing is None:
                    state.journal.write(name, self.record, create=True)
            self.record = {
                "schema": 1,
                "scope": SCOPE,
                "anchor": deepcopy(self.host.anchor),
                "nonce": nonce,
                "name": "praximesh-local-control-" + nonce,
                "image": image,
                "image_config": config,
                "hello": greeting,
                "arguments": None,
                "phase": "prepared",
                "id": None,
                "created_at": None,
                "started_at": None,
                "runtime_sha256": None,
            }
            self.record["arguments"] = arguments(self.host, self.record)
            # Check the name before recording any claim over it.
            raw = self.host.engine.run(
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "name=^/" + self.record["name"] + "$",
            ).stdout
            require(not raw.strip(), "An existing controller name cannot be adopted")
            self.save("prepared")
            self.fresh = True
            return deepcopy(operation)

    def check_fresh(self):
        require(self.fresh, "Recovered controller intents may only be cleaned, never started")
        value = self.record["hello"]
        require(
            canonical_digest(value)
            == canonical_digest(
                hello(
                    value["mode"],
                    self.host.device,
                    value["nonce"],
                    value["deadline"],
                    value["binding"],
                )
            ),
            "Current controller sources changed before startup",
        )

    def register(self, info):
        require(
            stopped(info)
            and info["State"]["Status"] == "created"
            and info["State"]["StartedAt"] in ZERO,
            "Unregistered controller was already started",
        )
        self.record.update(
            id=info["Id"], created_at=info["Created"], runtime_sha256=fingerprint(info)
        )
        self.save("created")

    def create(self):
        require(
            self.record is not None and self.record["phase"] == "prepared",
            "A fresh original creation intent is required",
        )
        with self.host.physical():
            self.check_fresh()
            require(
                time.monotonic() < self.record["hello"]["deadline"], "Original invocation expired"
            )
            info = self.present()
            if info is None:
                raw = (
                    self.command("container", "create", *self.record["arguments"]).decode().strip()
                )
                require(HEX_ID.fullmatch(raw), "Controller creation did not return a complete ID")
                info = self.present()
                require(
                    info is not None and info["Id"] == raw, "Controller creation response changed"
                )
            self.register(info)

    def request_start(self):
        require(
            self.record is not None and self.record["phase"] == "created",
            "Only the original unstarted controller may start",
        )
        with self.host.physical():
            self.check_fresh()
            info = self.present()
            require(
                info is not None
                and stopped(info)
                and info["State"]["StartedAt"] in ZERO
                and info["State"]["Status"] == "created"
                and time.monotonic() < self.record["hello"]["deadline"],
                "Original controller cannot start",
            )
            self.save("start_requested")
        # The fixed child acquires the same physical lock after this return.
        return self.record["id"]

    def observe(self):
        require(
            self.record is not None
            and self.record["phase"] in {"start_requested", "running", "stop_requested"},
            "No original startup to observe",
        )
        info = self.present()
        require(info is not None, "Registered controller is missing; retain original state")
        if info["State"]["StartedAt"] not in ZERO and self.record["started_at"] is None:
            self.record["started_at"] = info["State"]["StartedAt"]
            self.save(
                "running" if self.record["phase"] == "start_requested" else self.record["phase"]
            )
        return info

    def remove(self):
        """Stop only the frozen original; never acquire its physical lock first."""
        require(self.record is not None, "Missing original invocation")
        info = self.present()
        if self.record["phase"] in {"removed", "completed"}:
            require(info is None, "A controller reappeared after removal")
            return
        if info is None:
            require(
                self.record["phase"] in {"prepared", "remove_requested"},
                "Registered controller disappeared before removal intent",
            )
            self.save("removed")
            return
        if self.record["id"] is None:
            self.register(info)
        elif self.record["phase"] == "created":
            require(
                info["State"]["StartedAt"] in ZERO and stopped(info),
                "Controller started before its durable intent",
            )
        if info["State"]["StartedAt"] not in ZERO and self.record["started_at"] is None:
            self.record["started_at"] = info["State"]["StartedAt"]
        if self.record["phase"] != "remove_requested":
            self.save("stop_requested")
            if not stopped(info):
                require(
                    info["State"].get("Running") is True
                    and info["State"].get("Paused") is False
                    and info["State"].get("Restarting") is False,
                    "Unknown original controller state",
                )
                self.command("container", "kill", "--signal=KILL", self.record["id"])
            info = self.present()
            require(info is not None and stopped(info), "Original controller stop is unconfirmed")
            self.save("remove_requested")
        require(stopped(info), "Original controller restarted during removal")
        self.command("container", "rm", self.record["id"])
        require(self.present() is None, "Original controller removal is unconfirmed")
        self.save("removed")
