"""Owned containers for trusted supervision; not connected to GPU admission."""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from judge.container_contract import (
    ENTRY,
    MAX_SECONDS,
    PROTOCOL,
    PYTHON,
    START_SECONDS,
    STATE,
    STATUS_BYTES,
    STOP_SECONDS,
    SUPERVISION_PROFILE,
    TMP_BYTES,
    USER,
    docker_resources,
    resource_limits,
    resource_marker,
    source_digest,
)
from judge.gpu_command import run_command
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable

PREFIX = "io.praximesh.supervision."
TMPFS = f"rw,noexec,nosuid,nodev,size={TMP_BYTES},mode=0700,uid=65532,gid=65532"
HEX_ID = re.compile(r"[a-f0-9]{64}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


class LocalEngine:
    """Explicit private local socket; no context, proxy, pull or daemon mutation."""

    def __init__(self, socket_path):
        self.path = Path(socket_path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("An absolute local Docker socket is required")
        self.identity = None
        self.check_socket()

    def check_socket(self):
        info = self.path.lstat()
        parent = self.path.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise SandboxUnavailable("Docker socket must be owned under a private directory")
        identity = info.st_dev, info.st_ino
        if self.identity is not None and self.identity != identity:
            raise SandboxUnavailable("Docker socket changed; explicit recovery is required")
        self.identity = identity

    def run(self, *args, on_tick=lambda: None, check=True):
        self.check_socket()
        # Even docker create can otherwise inject configured proxy credentials.
        with tempfile.TemporaryDirectory(prefix="px-docker-client-") as config:
            result = run_command(
                ["docker", "--config", config, "--host", "unix://" + str(self.path), *args],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                deadline=time.monotonic() + 4,
                seconds=4,
                on_tick=on_tick,
                check=check,
                max_output=256 * 1024,
            )
        self.check_socket()
        return result

    def verify(self):
        try:
            info = json.loads(self.run("info", "--format", "{{json .}}").stdout)
            if (
                "name=rootless" not in info["SecurityOptions"]
                or info["CgroupVersion"] != "2"
                or info["CgroupDriver"] != "systemd"
                or any(
                    info[key] is not True
                    for key in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota")
                )
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise SandboxUnavailable("Verified rootless cgroup v2 limits are required") from None


class ContainerService:
    SCOPE = PROTOCOL

    def required_image_labels(self):
        return {PREFIX + "protocol": PROTOCOL}

    def valid_security_options(self, options):
        return options == ["no-new-privileges"]

    @contextmanager
    def creation_options(self):
        yield ()

    def record_intent(self, arguments):
        pass

    def record_created(self):
        pass

    def record_started(self, status):
        pass

    def __init__(
        self,
        guard,
        job_id,
        reservation_token,
        *,
        engine,
        image,
        purpose="task",
        resource_profile=SUPERVISION_PROFILE,
    ):
        if type(guard) is not DeviceLock:
            raise ValueError("An owned device lock is required")
        guard.assert_held()
        resource_limits(resource_profile)
        self.resource_profile = resource_profile
        if (
            type(image) is not str
            or not IMAGE_ID.fullmatch(image)
            or purpose not in {"task", "recovery", "maintenance"}
        ):
            raise ValueError("A local immutable image ID and known service purpose are required")
        self.guard, self.engine, self.image = guard, engine, image
        self.job_id, self.reservation_token = (
            uuid.UUID(str(job_id)),
            uuid.UUID(str(reservation_token)),
        )
        self.owner = digest(
            [
                self.SCOPE,
                os.getuid(),
                guard.device_uuid,
                str(self.job_id),
                str(self.reservation_token),
                purpose,
            ]
        )
        self.name = "praximesh-gpu-container-" + self.owner[:40]
        self.container_id = self.started_at = self.image_env = None

    def _run(self, *args, **kwargs):
        self.guard.assert_held()
        result = self.engine.run(*args, **kwargs)
        self.guard.assert_held()
        return result

    def _verify_image(self):
        if self.image_env is None:
            try:
                records = json.loads(self._run("image", "inspect", self.image).stdout)
                if type(records) is not list or len(records) != 1:
                    raise ValueError
                image = records[0]
                config = image["Config"]
                labels = config.get("Labels") or {}
                environment = config.get("Env") or []
                if (
                    image["Id"] != self.image
                    or image["Os"] != "linux"
                    or type(labels) is not dict
                    or any(
                        labels.get(key) != value
                        for key, value in self.required_image_labels().items()
                    )
                    or type(environment) is not list
                    or any(type(item) is not str for item in environment)
                    or config.get("Entrypoint") != [PYTHON, "-I", ENTRY]
                    or config.get("Volumes")
                    or config.get("OnBuild")
                    or config.get("Healthcheck")
                ):
                    raise ValueError
                self.image_env = environment
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                raise SandboxUnavailable(
                    "Image does not implement the trusted supervisor contract"
                ) from None

    def inspect(self):
        target = self.container_id or self.name
        result = self._run("container", "inspect", target, check=False)
        if result.returncode:
            # Distinguish absence from CLI/daemon failure; do not infer it from stderr.
            candidates = self._run(
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "name=^/" + self.name + "$",
                "--quiet",
            ).stdout.strip()
            if candidates:
                raise SandboxUnavailable("Container identity disappeared or was replaced")
            return None
        try:
            records = json.loads(result.stdout)
            if type(records) is not list or len(records) != 1:
                raise ValueError
            record = records[0]
            self._verify_image()
            config, host, state = record["Config"], record["HostConfig"], record["State"]
            labels, arguments = config["Labels"], config["Cmd"]
            container_id = record["Id"]
            before, deadline = float(arguments[0]), float(arguments[1])
            if (
                not HEX_ID.fullmatch(container_id)
                or (self.container_id is not None and container_id != self.container_id)
                or record["Name"] != "/" + self.name
                or record["Image"] != self.image
                or record.get("RestartCount") != 0
                or labels
                != {
                    **self.required_image_labels(),
                    PREFIX + "owner": self.owner,
                    PREFIX + "command": digest(arguments),
                }
                or len(arguments) < 5
                or arguments[3] != resource_marker(self.resource_profile)
                or not re.fullmatch("[a-f0-9]{32}", arguments[2])
                or not Path(arguments[4]).is_absolute()
                or not math.isfinite(before)
                or not math.isfinite(deadline)
                or not 0 < deadline - before <= MAX_SECONDS
                or config["Entrypoint"] != [PYTHON, "-I", ENTRY]
                or config["User"] != USER
                or config["WorkingDir"] != STATE
                or config["StopSignal"] != "SIGKILL"
                or config["StopTimeout"] != STOP_SECONDS
                or (config.get("Env") or []) != self.image_env
                or record["Mounts"]
                or state["Status"] not in {"created", "running", "exited"}
                or state["Paused"]
                or state["Restarting"]
                or state["Dead"]
                or type(state["Running"]) is not bool
                or type(state["OOMKilled"]) is not bool
                or state["Running"] != (state["Status"] == "running")
                or type(state["Pid"]) is not int
                or (state["Pid"] <= 0 if state["Running"] else state["Pid"] != 0)
            ):
                raise ValueError
            expected = {
                "Runtime": "runc",
                "NetworkMode": "none",
                "PidMode": "",
                "IpcMode": "private",
                "UTSMode": "",
                "CgroupnsMode": "private",
                "Privileged": False,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                **docker_resources(self.resource_profile),
                "NanoCpus": 0,
                "AutoRemove": False,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "LogConfig": {"Type": "none", "Config": {}},
                "Tmpfs": {STATE: TMPFS},
            }
            if (
                not self.valid_security_options(host.get("SecurityOpt"))
                or any(host.get(key) != value for key, value in expected.items())
                or any(
                    host.get(key)
                    for key in (
                        "Binds",
                        "Mounts",
                        "VolumesFrom",
                        "Devices",
                        "DeviceRequests",
                        "DeviceCgroupRules",
                        "CapAdd",
                        "GroupAdd",
                        "Init",
                        "Links",
                        "CgroupParent",
                    )
                )
            ):
                raise ValueError
            started_at = state["StartedAt"]
            if self.started_at is not None and started_at != self.started_at:
                raise ValueError
            if state["Running"]:
                self.started_at = started_at
            self.container_id = container_id
            return {
                "id": container_id,
                "running": state["Running"],
                "status": state["Status"],
                "exit_code": state["ExitCode"],
                "oom_killed": state["OOMKilled"],
                "pid": state["Pid"],
                "arguments": arguments,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            raise SandboxUnavailable("Container ownership or containment does not match") from None

    def start(self, command, *, deadline, on_tick):
        self.guard.assert_held()
        if (
            type(command) not in {list, tuple}
            or not command
            or any(type(part) is not str or not part or "\0" in part for part in command)
            or not Path(command[0]).is_absolute()
        ):
            raise ValueError("A fixed trusted absolute controller command is required")
        if (
            not math.isfinite(deadline)
            or not START_SECONDS + STOP_SECONDS < deadline - time.monotonic() <= MAX_SECONDS
        ):
            raise SandboxUnavailable("Insufficient bounded task reservation")
        self.engine.verify()
        self._verify_image()
        if self.inspect() is not None:
            raise SandboxUnavailable("Container already exists; recover it first")
        on_tick()
        before, until = time.monotonic() + START_SECONDS, deadline - STOP_SECONDS
        if before >= until:
            raise SandboxUnavailable("Reservation expired before creation")
        arguments = [
            str(before),
            str(until),
            uuid.uuid4().hex,
            resource_marker(self.resource_profile),
            *command,
        ]
        limits = docker_resources(self.resource_profile)
        self.record_intent(arguments)
        with self.creation_options() as extra_options:
            created = (
                self._run(
                    "container",
                    "create",
                    "--pull=never",
                    "--name",
                    self.name,
                    "--label",
                    PREFIX + "protocol=" + PROTOCOL,
                    "--label",
                    PREFIX + "owner=" + self.owner,
                    "--label",
                    PREFIX + "command=" + digest(arguments),
                    "--user",
                    USER,
                    "--workdir",
                    STATE,
                    "--network=none",
                    "--runtime=runc",
                    "--ipc=private",
                    "--cgroupns=private",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    *extra_options,
                    "--memory",
                    str(limits["Memory"]),
                    "--memory-swap",
                    str(limits["MemorySwap"]),
                    "--cpu-period",
                    str(limits["CpuPeriod"]),
                    "--cpu-quota",
                    str(limits["CpuQuota"]),
                    "--pids-limit",
                    str(limits["PidsLimit"]),
                    "--restart=no",
                    "--stop-signal=SIGKILL",
                    "--stop-timeout",
                    str(STOP_SECONDS),
                    "--log-driver=none",
                    "--tmpfs",
                    STATE + ":" + TMPFS,
                    self.image,
                    *arguments,
                    on_tick=on_tick,
                )
                .stdout.decode()
                .strip()
            )
        if not HEX_ID.fullmatch(created):
            raise SandboxUnavailable("Container creation did not return an identity")
        self.container_id = created
        self.inspect()
        self.record_created()
        if time.monotonic() >= before:
            raise SandboxUnavailable("Container missed its startup window; recover it")
        self._run("container", "start", self.container_id, on_tick=on_tick)
        while time.monotonic() < before:
            state = self.inspect()
            if state is None or not state["running"]:
                raise SandboxUnavailable("Trusted supervisor exited before readiness")
            result = self._run(
                "container",
                "exec",
                self.container_id,
                PYTHON,
                "-I",
                "-c",
                f"from pathlib import Path; import sys; sys.stdout.buffer.write(Path('{STATE}/status.json').read_bytes()[:{STATUS_BYTES + 1}])",
                check=False,
                on_tick=on_tick,
            )
            if result.returncode == 0:
                try:
                    status = json.loads(result.stdout)
                    if (
                        len(result.stdout) > STATUS_BYTES
                        or set(status)
                        != {"protocol", "source_digest", "nonce", "deadline", "child", "limits"}
                        or status["protocol"] != PROTOCOL
                        or status["source_digest"] != source_digest()
                        or status["nonce"] != arguments[2]
                        or status["deadline"] != until
                        or type(status["child"]) is not int
                        or status["child"] <= 1
                        or status["limits"] != resource_limits(self.resource_profile)
                    ):
                        raise ValueError
                except (TypeError, KeyError, ValueError):
                    raise SandboxUnavailable("Invalid trusted supervisor readiness") from None
                current = self.inspect()
                if current is None or not current["running"] or time.monotonic() >= until:
                    raise SandboxUnavailable("Supervisor ended before readiness was confirmed")
                self.record_started(status)
                return status
            on_tick()
            time.sleep(0.05)
        raise SandboxUnavailable("Trusted supervisor did not become ready in time")

    def stop(self):
        state = self.inspect()
        if state is None:
            return
        if state["running"]:
            self._run("container", "kill", "--signal=KILL", state["id"], check=False)
        end = time.monotonic() + STOP_SECONDS
        while time.monotonic() < end:
            state = self.inspect()
            if state is None or not state["running"]:
                return
            time.sleep(0.05)
        raise SandboxUnavailable("Owned container did not stop; recovery is required")

    def remove(self):
        state = self.inspect()
        if state is None:
            return
        if state["running"]:
            raise SandboxUnavailable("Stop the owned container before removal")
        self._run("container", "rm", state["id"])
        if self.inspect() is not None:
            raise SandboxUnavailable("Owned container removal was not confirmed")
