"""Internal, journalled GPU worker/preflight lifecycle; no activation CLI.

Creation and configuration checks hold the original device lock. Starting a
worker releases that lock so the fixed worker can acquire it itself. Stopping a
container never releases its task records, physical reservations or readiness.
"""

import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime

from judge.container_controller import SOCKET, STATE, identity
from judge.container_deployment_config import OWNER, canonical, digest
from judge.container_journal import private
from judge.container_processes import HOST_PROC
from judge.container_worker_config import read_config
from scripts.deploy_gpu import GPUDeploymentNode, read_private
from scripts.deploy_gpu_config import context, frozen
from scripts.deploy_web import environment_values, fingerprint
from scripts.release_gpu import check_image

LABEL = "org.praximesh.gpu-worker-intent"
HEX = re.compile(r"[a-f0-9]{64}")
PHASES = {
    "prepared",
    "created",
    "start_requested",
    "running",
    "completed",
    "start_failed",
    "stop_requested",
    "stopped",
}
FIELDS = {
    "schema",
    "token",
    "mode",
    "generation",
    "binding_sha256",
    "metadata",
    "network",
    "image_config_sha256",
    "arguments",
    "id",
    "runtime_sha256",
    "phase",
    "started_at",
    "launch_before",
    "operation",
}
TMPFS = "rw,noexec,nosuid,nodev,size=16777216,mode=1777"
ZERO = {"0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000000Z"}


class WorkerLifecycleError(RuntimeError):
    pass


def started(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value
    ):
        raise WorkerLifecycleError("Worker startup identity is missing")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def stopped(info):
    state = info["State"]
    return (
        state.get("Running") is False
        and state.get("Paused") is False
        and state.get("Restarting") is False
        and state.get("Pid") == 0
        and state.get("Status") in {"created", "exited"}
    )


def running(info):
    state = info["State"]
    return (
        state.get("Running") is True
        and state.get("Paused") is False
        and state.get("Restarting") is False
        and type(state.get("Pid")) is int
        and state["Pid"] > 0
        and state.get("Status") == "running"
    )


class WorkerRuntime:
    def __init__(self, host, generation, *, mode="worker"):
        if mode not in {"worker", "check"}:
            raise ValueError("Expected the fixed worker or no-device check")
        host.require_locked()
        host.check_gpu_attachment()
        self.host, self.generation, self.mode = host, context(generation), mode
        self.binding = host.state["gpu_attachment"]["binding"]
        self.role = "gpu_" + mode
        self.name = generation["gpu_worker"] + ("-check" if mode == "check" else "")
        self.node = GPUDeploymentNode(
            host.config["gpu"],
            host.directory,
            host.state["site_id"],
            self.binding["node"]["installation"]["engine"],
        )
        self.record = None

    def command(self, *args, timeout=15):
        """Use the original socket with an empty private Docker client config."""
        self.host.require_locked()
        self.node.engine.check_socket()
        if type(timeout) is not int or not 1 <= timeout <= 255:
            raise WorkerLifecycleError("Invalid worker control deadline")
        with tempfile.TemporaryDirectory(prefix="px-gpu-worker-client-") as directory:
            try:
                output = self.host.run(
                    "--config",
                    directory,
                    "--host",
                    "unix://" + str(self.node.engine.path),
                    *args,
                    environment={
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "LC_ALL": "C.UTF-8",
                    },
                    timeout=timeout,
                )
                if type(output) is not bytes or len(output) > 256 * 1024:
                    raise ValueError("Unexpected control response")
            except Exception:
                raise WorkerLifecycleError("Original worker container operation failed") from None
        self.node.engine.check_socket()
        return output

    def configuration(self, *, current=True):
        """Read-only original identity; this does not authorize lifecycle mutations."""
        self.host.require_locked()
        operation = self.host.state.get("operation") or {}
        token = operation.get("id")
        try:
            valid = type(token) is str and str(uuid.UUID(token)) == token
        except ValueError:
            valid = False
        if not valid:
            raise WorkerLifecycleError(
                "Worker lifecycle requires the original operation and maintenance route"
            )
        if self.node.engine.verify()["ID"] != self.binding["node"]["installation"]["engine"]:
            raise WorkerLifecycleError("Worker lifecycle daemon identity changed")
        private(self.host.directory.lstat(), directory=True)
        if (
            self.binding["coordinator"]
            != {"path": str(self.host.directory), "identity": identity(self.host.directory)}
            or self.binding["node"]["config"] != self.host.config["gpu"]
            or self.binding["site_id"] != self.host.state["site_id"]
        ):
            raise WorkerLifecycleError("Worker lifecycle original binding changed")
        fd = os.open(self.node.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if canonical(json.loads(read_private(fd, OWNER))) != canonical(self.binding):
                raise WorkerLifecycleError("Worker lifecycle ownership marker changed")
        finally:
            os.close(fd)
        config, metadata = read_config(self.node.root, with_generation=True)
        active = self.host.state.get("gpu_configuration") or {}
        if (
            active.get("transition") is not None
            or active.get("active") is None
            or metadata != active["active"]["metadata"]
        ):
            raise WorkerLifecycleError("Finish the original GPU configuration publication first")
        if current and active["active"]["generation"] != self.generation:
            raise WorkerLifecycleError("Worker must use its exact published generation")
        return config, metadata, token

    def common(self, *, current=True):
        config, metadata, token = self.configuration(current=current)
        if self.host.proxy.probe()["port"] is not None:
            raise WorkerLifecycleError(
                "Worker lifecycle requires the original operation and maintenance route"
            )
        self.host.database.gpu_worker_pause(self.generation["database"], token)
        return config, metadata, token

    def load(self):
        values = self.host.state.get("gpu_runtime", {})
        if type(values) is not dict:
            raise WorkerLifecycleError("Invalid worker lifecycle journal")
        record = values.get(self.name)
        if record is None:
            self.record = None
            return None
        try:
            valid = (
                type(record) is dict
                and set(record) == FIELDS
                and type(record["schema"]) is int
                and record["schema"] == 1
                and re.fullmatch(r"[a-f0-9]{32}", record["token"])
                and record["mode"] == self.mode
                and record["generation"] == self.generation
                and record["binding_sha256"] == digest(self.binding)
                and record["phase"] in PHASES
                and record["arguments"] == self.arguments(record)
                and HEX.fullmatch(record["image_config_sha256"])
                and (record["id"] is None or HEX.fullmatch(record["id"]))
                and (record["runtime_sha256"] is None or HEX.fullmatch(record["runtime_sha256"]))
                and (
                    record["phase"] == "prepared"
                    or (record["id"] is not None and record["runtime_sha256"] is not None)
                )
                and (
                    record["phase"] != "prepared"
                    or (record["id"] is None and record["runtime_sha256"] is None)
                )
                and str(uuid.UUID(record["operation"])) == record["operation"]
            )
            for name in ("started_at", "launch_before"):
                if record[name] is not None:
                    started(record[name])
        except (KeyError, TypeError, AttributeError, ValueError):
            valid = False
        if not valid:
            raise WorkerLifecycleError("Worker lifecycle does not match its original intent")
        self.record = record
        return record

    def save(self, phase):
        self.record["phase"] = phase
        self.host.state.setdefault("gpu_runtime", {})[self.name] = self.record
        self.host.save()

    def network(self):
        name = self.host.config["networks"]["backend"]
        values = json.loads(self.command("network", "inspect", name))
        if (
            type(values) is not list
            or len(values) != 1
            or values[0]["Name"] != name
            or not HEX.fullmatch(values[0]["Id"])
            or values[0]["Internal"] is not True
            or values[0]["Driver"] != "bridge"
        ):
            raise WorkerLifecycleError("Worker needs the original internal database network")
        return {"name": name, "id": values[0]["Id"]}

    def image(self):
        expected = self.generation["manifest"]["images"]["gpu_worker"]
        values = json.loads(self.command("image", "inspect", expected))
        if (
            type(values) is not list
            or len(values) != 1
            or values[0]["Id"] != expected
            or values[0].get("Architecture") != "amd64"
            or values[0].get("Os") != "linux"
        ):
            raise WorkerLifecycleError("Worker image differs from the frozen release")
        config = values[0]["Config"]
        check_image("gpu_worker", config, self.generation["manifest"]["gpu"])
        if config.get("Volumes") or config.get("Healthcheck"):
            raise WorkerLifecycleError(
                "Worker image must not add mounts or executable health checks"
            )
        return config

    def labels(self, record):
        return {
            "org.praximesh.site": self.host.state["site_id"],
            "org.praximesh.generation": self.generation["id"],
            "org.praximesh.role": self.role,
            "org.praximesh.gpu-binding": digest(self.binding),
            LABEL: record["token"],
        }

    def arguments(self, record):
        result = [
            "--pull=never",
            "--runtime=runc",
            "--name",
            self.name,
            "--hostname",
            self.name,
            "--user=0:0",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            record["network"]["id"],
            "--ipc=private",
            "--cgroupns=private",
            "--pids-limit=128",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1",
            "--restart=no",
            "--stop-timeout=240",
            "--log-driver=local",
            "--log-opt=max-size=1m",
            "--log-opt=max-file=1",
            "--log-opt=compress=false",
            "--tmpfs=/tmp:" + TMPFS,
        ]
        for key, value in self.labels(record).items():
            result += ["--label", key + "=" + value]
        result += [
            "--mount",
            f"type=bind,source={self.node.root},target={STATE}"
            + (",readonly" if self.mode == "check" else ""),
        ]
        if self.mode == "worker":
            # Static, previously bound physical minor; never query or select a device here.
            fd = os.open(self.node.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                minor = json.loads(read_private(fd, "mounts.json"))["device_observer"]["minor"]
            finally:
                os.close(fd)
            if type(minor) is not int or not 0 <= minor < 256:
                raise WorkerLifecycleError("Invalid original management device minor")
            result += [
                "--mount",
                f"type=bind,source={self.node.engine.path},target={SOCKET},readonly",
                "--mount",
                f"type=bind,source=/proc,target={HOST_PROC},readonly,bind-recursive=readonly,bind-propagation=rprivate",
                f"--device=/dev/nvidia{minor}:/dev/nvidia{minor}:rw",
                "--device=/dev/nvidiactl:/dev/nvidiactl:rw",
            ]
        result += [self.generation["manifest"]["images"]["gpu_worker"]]
        if self.mode == "check":
            result += ["--check"]
        return result

    def validate(self, info, image):
        record = self.record
        if record is None:
            raise WorkerLifecycleError("Missing durable creation intent")
        config, runtime = info["Config"], info["HostConfig"]
        devices = []
        if self.mode == "worker":
            for argument in record["arguments"]:
                if argument.startswith("--device="):
                    source, target, permissions = argument.removeprefix("--device=").split(":")
                    devices.append(
                        {
                            "PathOnHost": source,
                            "PathInContainer": target,
                            "CgroupPermissions": permissions,
                        }
                    )
        required = {
            "Runtime": "runc",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": record["network"]["id"],
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "NanoCpus": 1000000000,
            "PidsLimit": 128,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": {"/tmp": TMPFS},
            "Devices": devices,
            "LogConfig": {
                "Type": "local",
                "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
            },
        }
        mounts = {str(STATE): (str(self.node.root), self.mode == "worker")}
        if self.mode == "worker":
            mounts |= {
                str(SOCKET): (str(self.node.engine.path), False),
                str(HOST_PROC): ("/proc", False),
            }
        labels = config.get("Labels") or {}
        expected_labels = (image.get("Labels") or {}) | self.labels(record)
        valid = (
            HEX.fullmatch(info["Id"])
            and info["Name"] == "/" + self.name
            and info["Image"] == self.generation["manifest"]["images"]["gpu_worker"]
            and (record["id"] is None or info["Id"] == record["id"])
            and digest(image) == record["image_config_sha256"]
            and config.get("User") == "0:0"
            and config.get("Hostname") == self.name
            and config.get("Entrypoint") == image["Entrypoint"]
            and (
                config.get("Cmd") == ["--check"]
                if self.mode == "check"
                else config.get("Cmd") in (None, [])
            )
            and config.get("WorkingDir") == image.get("WorkingDir")
            and config.get("StopSignal") == image.get("StopSignal")
            and config.get("StopTimeout") == 240
            and labels == expected_labels
            and environment_values(config.get("Env")) == environment_values(image.get("Env"))
            and all(runtime.get(key) == value for key, value in required.items())
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
            and not any(
                config.get(key)
                for key in ("Volumes", "Healthcheck", "ExposedPorts", "Tty", "OpenStdin")
            )
            and len(info["Mounts"]) == len(mounts)
            and {m["Destination"]: (m["Source"], m["RW"]) for m in info["Mounts"]} == mounts
            and all(m["Type"] == "bind" and m["Propagation"] == "rprivate" for m in info["Mounts"])
            and set(info["NetworkSettings"].get("Networks", {})) == {record["network"]["name"]}
            and not any(info["NetworkSettings"].get("Ports", {}).values())
        )
        if self.mode == "worker":
            proc = [m for m in runtime.get("Mounts", []) if m.get("Target") == str(HOST_PROC)]
            valid = valid and len(proc) == 1 and proc[0].get("ReadOnly") is True
            options = proc[0].get("BindOptions", {}) if len(proc) == 1 else {}
            valid = (
                valid
                and options.get("ReadOnlyForceRecursive") is True
                and options.get("Propagation") == "rprivate"
                and not options.get("NonRecursive")
                and not options.get("ReadOnlyNonRecursive")
            )
        if not valid or (
            record["runtime_sha256"] is not None and fingerprint(info) != record["runtime_sha256"]
        ):
            raise WorkerLifecycleError("Worker container differs from its fixed creation contract")
        started(info["State"]["StartedAt"])
        return info

    def inspect(self, *, absent=False):
        # A successful full listing distinguishes absence from an inspect failure.
        ids = (
            self.command(
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "name=^/" + self.name + "$",
            )
            .decode()
            .split()
        )
        if len(ids) > 1 or any(not HEX.fullmatch(value) for value in ids):
            raise WorkerLifecycleError("Ambiguous worker container name")
        record = self.record
        if not ids:
            if absent and record["id"] is None:
                return None
            raise WorkerLifecycleError("Recorded worker container is missing; it was not recreated")
        values = json.loads(self.command("container", "inspect", ids[0]))
        if len(values) != 1 or values[0]["Id"] != ids[0]:
            raise WorkerLifecycleError("Original worker inspection is inconsistent")
        if self.network() != record["network"]:
            raise WorkerLifecycleError("Original database network changed")
        return self.validate(values[0], self.image())

    def register(self, info):
        self.record["id"], self.record["runtime_sha256"] = info["Id"], fingerprint(info)
        key = "gpu_workers" if self.mode == "worker" else "gpu_checks"
        self.host.state.setdefault(key, {})[self.generation["id"]] = {
            "id": info["Id"],
            "name": self.name,
            "image": info["Image"],
            "binding_sha256": digest(self.binding),
            "runtime_sha256": fingerprint(info),
        }
        self.save("created")

    def create(self):
        _, metadata, token = self.common()
        record = self.load()
        if record is not None and record["phase"] != "prepared":
            if record["metadata"] != metadata:
                raise WorkerLifecycleError("Worker creation configuration changed")
            return self.inspect()
        with self.node.locked() as node:
            node.require_owner(self.binding)
            self.common()
            if record is None:
                candidates = [
                    self.host.state.get("current"),
                    (self.host.state.get("operation") or {}).get("previous"),
                    (self.host.state.get("operation") or {}).get("candidate"),
                ]
                if not any(
                    value and "gpu_worker" in value and context(value) == self.generation
                    for value in candidates
                ):
                    raise WorkerLifecycleError(
                        "Worker creation requires a recorded deployment generation"
                    )
                self.record = {
                    "schema": 1,
                    "token": uuid.uuid4().hex,
                    "mode": self.mode,
                    "generation": frozen(self.generation),
                    "binding_sha256": digest(self.binding),
                    "metadata": metadata,
                    "network": self.network(),
                    "image_config_sha256": digest(self.image()),
                    "arguments": None,
                    "id": None,
                    "runtime_sha256": None,
                    "phase": "prepared",
                    "started_at": None,
                    "launch_before": None,
                    "operation": token,
                }
                self.record["arguments"] = self.arguments(self.record)
                # Reject existing names before recording a new ownership intent.
                if self.command(
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "name=^/" + self.name + "$",
                ).strip():
                    raise WorkerLifecycleError("An existing worker name cannot be adopted")
                self.save("prepared")
            record = self.record
            if record["metadata"] != metadata:
                raise WorkerLifecycleError("Worker creation configuration changed")
            if record["phase"] != "prepared":
                return self.inspect()
            if record["operation"] != token:
                raise WorkerLifecycleError("Finish the original worker creation operation first")
            info = self.inspect(absent=True)
            if info is None:
                self.common()
                raw = (
                    self.command("container", "create", *record["arguments"], timeout=30)
                    .decode()
                    .strip()
                )
                if not HEX.fullmatch(raw):
                    raise WorkerLifecycleError("Worker creation did not return a complete ID")
                info = self.inspect()
                if info["Id"] != raw:
                    raise WorkerLifecycleError("Worker creation result changed before registration")
            if (
                not stopped(info)
                or info["State"]["Status"] != "created"
                or info["State"]["StartedAt"] not in ZERO
            ):
                raise WorkerLifecycleError("Unregistered worker was already started")
            self.register(info)
            return info

    def preflight(self, previous):
        check = WorkerRuntime(self.host, self.generation, mode="check")
        record = check.load()
        if (
            record is None
            or record["phase"] != "completed"
            or record["metadata"] != self.record["metadata"]
            or record["operation"] != self.host.state["operation"]["id"]
        ):
            raise WorkerLifecycleError("A completed fixed no-device preflight is required")
        info = check.inspect()
        if not check.success(info) or info["State"]["StartedAt"] != record["started_at"]:
            raise WorkerLifecycleError("The original no-device preflight result changed")
        if previous["State"]["StartedAt"] not in ZERO:
            finished = started(previous["State"].get("FinishedAt"))
            if finished in ZERO or datetime.fromisoformat(
                record["started_at"].replace("Z", "+00:00")
            ) < datetime.fromisoformat(finished.replace("Z", "+00:00")):
                raise WorkerLifecycleError(
                    "A fresh preflight after the previous worker stop is required"
                )

    def success(self, info):
        state = info["State"]
        return (
            stopped(info)
            and state["Status"] == "exited"
            and state.get("ExitCode") == 0
            and state.get("OOMKilled") is False
            and state.get("Error") == ""
            and state["StartedAt"] not in ZERO
            and started(state.get("FinishedAt")) not in ZERO
            and datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
            >= datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
        )

    def confirm_start(self, info):
        record, state = self.record, info["State"]
        if state["StartedAt"] == record["launch_before"] or state["StartedAt"] in ZERO:
            raise WorkerLifecycleError("Original worker has not completed this start request")
        record["started_at"] = state["StartedAt"]
        if running(info):
            self.save("running")
        elif self.mode == "check" and self.success(info):
            self.save("completed")
        else:
            self.save("start_failed")
            raise WorkerLifecycleError(
                "Original worker exited during startup; no readiness was accepted"
            )
        return info

    def start(self):
        config, metadata, token = self.common()
        record = self.load()
        if record is None or record["metadata"] != metadata:
            raise WorkerLifecycleError(
                "Create the original worker for the published configuration first"
            )
        info = self.inspect()
        if record["phase"] in {"running", "completed"}:
            if info["State"]["StartedAt"] != record["started_at"]:
                raise WorkerLifecycleError("Original worker startup instance changed")
            if record["phase"] == "completed" and not self.success(info):
                raise WorkerLifecycleError("Original preflight completion changed")
            if record["phase"] == "running" and not running(info):
                if self.mode == "check" and self.success(info):
                    self.save("completed")
                else:
                    self.save("start_failed")
                    raise WorkerLifecycleError(
                        "Original worker running state changed; recovery must reconcile it first"
                    )
            return info
        if record["phase"] == "start_requested":
            if record["operation"] != token:
                raise WorkerLifecycleError("Finish the original worker start operation first")
            if info["State"]["StartedAt"] != record["launch_before"]:
                return self.confirm_start(info)
        elif record["phase"] not in {"created", "stopped"}:
            raise WorkerLifecycleError("Finish the original worker lifecycle transition first")
        elif (record["phase"] == "created" and info["State"]["StartedAt"] not in ZERO) or (
            record["phase"] == "stopped" and info["State"]["StartedAt"] != record["started_at"]
        ):
            raise WorkerLifecycleError("Worker ran outside its recorded startup intent")
        with self.node.locked() as node:
            node.require_owner(self.binding)
            config, metadata, token = self.common()
            if metadata != record["metadata"] or (
                self.mode == "worker" and config["enabled"] is not True
            ):
                raise WorkerLifecycleError(
                    "The published worker configuration does not authorize startup"
                )
            info = self.inspect()
            if not stopped(info):
                raise WorkerLifecycleError(
                    "Original worker was started outside its recorded intent"
                )
            if self.mode == "worker":
                self.preflight(info)
            if record["phase"] != "start_requested":
                record.update(launch_before=info["State"]["StartedAt"], operation=token)
                self.save("start_requested")
        # The fixed worker must acquire the original device lock itself.
        self.common()
        info = self.inspect()
        if info["State"]["StartedAt"] != record["launch_before"]:
            return self.confirm_start(info)
        if not stopped(info):
            raise WorkerLifecycleError("Worker changed before its recorded start")
        self.command("container", "start", record["id"], timeout=30)
        return self.confirm_start(self.inspect())

    def stop(self):
        _, _, token = self.common(current=False)
        record = self.load()
        if record is None or record["phase"] == "prepared":
            raise WorkerLifecycleError("Reconcile the original worker creation before stopping")
        info = self.inspect()
        observed = info["State"]["StartedAt"]
        if record["phase"] == "stop_requested":
            if record["operation"] != token or observed != record["started_at"]:
                raise WorkerLifecycleError(
                    "Worker stop requires the original operation and startup instance"
                )
        else:
            if record["phase"] == "created" and observed not in ZERO:
                raise WorkerLifecycleError("Worker ran outside its recorded startup intent")
            expected = (
                record["launch_before"]
                if record["phase"] == "start_requested"
                else record["started_at"]
            )
            if (
                record["phase"] != "start_requested"
                and expected is not None
                and observed != expected
            ):
                raise WorkerLifecycleError("Original worker startup instance changed before stop")
            record.update(started_at=observed, operation=token)
            self.save("stop_requested")
        self.common(current=False)
        info = self.inspect()
        if info["State"]["StartedAt"] != record["started_at"]:
            raise WorkerLifecycleError("Original worker restarted during stop")
        if not stopped(info):
            if (
                info["State"].get("Running") is not True
                or info["State"].get("Paused")
                or info["State"].get("Restarting")
            ):
                raise WorkerLifecycleError("Original worker is not in a controllable running state")
            self.command("container", "stop", "--timeout", "240", record["id"], timeout=255)
            info = self.inspect()
        if not stopped(info) or info["State"]["StartedAt"] != record["started_at"]:
            raise WorkerLifecycleError("Original worker stop is not confirmed")
        self.save("stopped")
        return info

    def check(self, *, seconds=60):
        if self.mode != "check":
            raise WorkerLifecycleError("Only the fixed no-device preflight may be awaited here")
        self.create()
        deadline = time.monotonic() + seconds
        while True:
            info = self.start()
            if self.record["phase"] == "completed":
                return info
            if time.monotonic() >= deadline:
                raise WorkerLifecycleError(
                    "Original preflight is still running; resume the same recorded container"
                )
            time.sleep(0.2)
