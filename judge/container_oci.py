"""Private runsc bundles inside the supervised trusted container.

Only trusted platform commands enter this API. No GPU or formal receipt is
provided. Resource limits cover the whole outer container, not a guest sub-cgroup.
"""

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from judge.container_contract import RESOURCE_PROFILES, STATE, resource_limits
from judge.container_resources import counters
from judge.gpu_artifacts import RUNSC_SHA256
from judge.gpu_command import run_command
from judge.gpu_session import GPUExecutionError, GPUSession
from judge.sandbox import SandboxUnavailable

RUNSC = "/opt/runsc/runsc"
GUEST_ROOT = "/opt/guest-root"
LABEL = re.compile(r"[a-z][a-z0-9-]{0,30}")


def specification(command):
    if (
        type(command) not in {list, tuple}
        or not command
        or any(type(part) is not str or not part or "\0" in part for part in command)
        or not Path(command[0]).is_absolute()
    ):
        raise ValueError("A fixed trusted absolute guest command is required")
    return {
        "ociVersion": "1.0.2",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": list(command),
            "env": [
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE=1",
                "HOME=/tmp",
                "LANG=C.UTF-8",
            ],
            "cwd": "/",
            "noNewPrivileges": True,
            "capabilities": {
                key: [] for key in ("bounding", "effective", "inheritable", "permitted", "ambient")
            },
            "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 256, "soft": 256}],
        },
        "root": {"path": GUEST_ROOT, "readonly": True},
        "hostname": "praximesh",
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {
                "destination": "/dev",
                "type": "tmpfs",
                "source": "tmpfs",
                "options": ["nosuid", "strictatime", "mode=755", "size=65536k"],
            },
            {
                "destination": "/tmp",
                "type": "tmpfs",
                "source": "tmpfs",
                "options": ["nosuid", "nodev", "noexec", "mode=1777", "size=32m"],
            },
            {
                "destination": "/sys",
                "type": "sysfs",
                "source": "sysfs",
                "options": ["nosuid", "noexec", "nodev", "ro"],
            },
        ],
        "linux": {
            "namespaces": [{"type": name} for name in ("pid", "network", "ipc", "uts", "mount")],
            "resources": {"devices": [{"allow": False, "access": "rwm"}]},
        },
    }


def processes():
    found = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            found[int(path.name)] = int(fields[19])
        except FileNotFoundError:
            pass
    return found


class ContainerRuntime:
    def __init__(self, *, deadline, on_tick, diagnostics=False):
        # Refuse a host invocation before starting any runtime process.
        if (
            os.getuid() != 65532
            or os.getgid() != 65532
            or Path("/proc/self/cgroup").read_text().strip() != "0::/"
            or not os.statvfs("/").f_flag & os.ST_RDONLY
        ):
            raise SandboxUnavailable("Expected the supervised private runtime container")
        supervision = json.loads((Path(STATE) / "status.json").read_text())
        profiles = [
            profile
            for profile in RESOURCE_PROFILES
            if supervision.get("limits") == resource_limits(profile)
        ]
        if len(profiles) != 1:
            raise SandboxUnavailable("Unknown supervised resource profile")
        self.resource_profile = profiles[0]
        expected = resource_limits(self.resource_profile)
        if any(
            (Path("/sys/fs/cgroup") / name).read_text().strip() != value
            for name, value in expected.items()
        ):
            raise SandboxUnavailable("Outer runtime limits changed")
        if (
            type(deadline) not in {int, float}
            or not math.isfinite(deadline)
            or not time.monotonic() < deadline <= supervision["deadline"]
        ):
            raise SandboxUnavailable("OCI deadline must fit the original supervision window")
        with Path(RUNSC).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != RUNSC_SHA256:
                raise SandboxUnavailable("Unexpected pinned runsc binary")
        self.base = Path(STATE) / "oci"
        self.base.mkdir(mode=0o700)
        self.deadline, self.on_tick = deadline, on_tick
        self.diagnostics = diagnostics
        self.containers = {}
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "GOMAXPROCS": "2",
            "TMPDIR": STATE,
        }

    def tick(self):
        if time.monotonic() >= self.deadline:
            raise SandboxUnavailable("Owned OCI runtime deadline expired")
        self.on_tick()
        if time.monotonic() >= self.deadline:
            raise SandboxUnavailable("Owned OCI runtime deadline expired")

    def arguments(self, *args):
        return [
            RUNSC,
            "--root=" + str(self.base / "state"),
            "--rootless=true",
            "--ignore-cgroups=true",
            "--network=none",
            "--platform=systrap",
            "--debug=false",
            "--directfs=false",
            *(["--debug-log=" + str(self.base / "runtime.log")] if self.diagnostics else []),
            *args,
        ]

    def command(self, *args):
        return run_command(
            self.arguments(*args),
            env=self.environment,
            deadline=self.deadline,
            seconds=4,
            on_tick=self.tick,
            max_output=65536,
        )

    def create(self, label, entrypoint, arguments):
        self.tick()
        if type(label) is not str or not LABEL.fullmatch(label) or label in self.containers:
            raise ValueError("An unused fixed container label is required")
        spec = specification([entrypoint, *arguments])
        bundle = self.base / label
        bundle.mkdir(mode=0o700)
        (bundle / "config.json").write_text(json.dumps(spec))
        self.containers[label] = {"bundle": bundle, "spec": spec, "process": None}
        return label

    def attach(self, label):
        self.tick()
        record = self.containers[label]
        path = record["bundle"] / "config.json"
        if (
            record["process"] is not None
            or not stat.S_ISREG(path.lstat().st_mode)
            or json.loads(path.read_text()) != record["spec"]
        ):
            raise SandboxUnavailable("Only an unchanged new owned OCI bundle may start")
        if any(
            other.get("process") is not None and not other.get("closed")
            for other in self.containers.values()
        ):
            raise SandboxUnavailable("Recover the previous owned guest before starting another")
        record["before"] = processes()
        process = subprocess.Popen(
            self.arguments("run", "--bundle=" + str(record["bundle"]), label),
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        record["process"] = process
        return process

    def inspect(self, label):
        record = self.containers[label]
        state = json.loads(self.command("state", label).stdout)
        if (
            state["id"] != label
            or state["bundle"] != str(record["bundle"])
            or state["status"] not in {"created", "running", "stopped"}
        ):
            raise SandboxUnavailable("Owned OCI runtime identity changed")
        return state

    def session(self, label, *, mode, on_tick, on_cleanup=None, architecture=()):
        initial_memory = counters("memory.events")
        cleaned = False

        def cleanup():
            nonlocal cleaned
            if not cleaned:
                result = self.stop_container(label)
                cleaned = True
                if on_cleanup is not None:
                    on_cleanup(result)

        def exit_verdict(_code):
            if process.poll() is not None and exceeded_memory():
                # runsc can remove its state when the sandbox is OOM-killed.
                # Confirm owned runtime/guest termination instead of querying a
                # possibly absent state, and never classify an uncleaned OOM.
                cleanup()
                return "memory_limit"
            return None

        def exceeded_memory():
            current = counters("memory.events")
            # oom_kill includes global OOMs too. Require this cgroup's own
            # limit/allocation events rather than blaming unrelated pressure.
            return all(current[key] > initial_memory[key] for key in ("max", "oom", "oom_kill"))

        def tick():
            on_tick()
            if exceeded_memory():
                # An OOM-killed sandbox need not promptly terminate its attach
                # client. Observe the kernel throughout the live session.
                cleanup()
                raise GPUExecutionError("memory_limit")

        process = self.attach(label)
        return GPUSession(
            process,
            cleanup,
            architecture=architecture,
            mode=mode,
            on_tick=tick,
            exit_verdict=exit_verdict,
        )

    def list_containers(self):
        states = json.loads(self.command("list", "--format=json").stdout)
        if states is None:  # runsc serializes an empty Go slice as JSON null.
            return []
        if type(states) is not list or any(
            type(item) is not dict or type(item.get("id")) is not str for item in states
        ):
            raise SandboxUnavailable("Invalid owned OCI runtime state list")
        return states

    def stop_container(self, label):
        record = self.containers[label]
        states = self.list_containers()
        if any(item["id"] == label for item in states):
            self.inspect(label)
            self.command("delete", "--force", label)
        states = self.list_containers()
        if any(item["id"] == label for item in states):
            raise SandboxUnavailable("Owned OCI container removal was not confirmed")
        process = record["process"]
        if process is not None:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                raise SandboxUnavailable(
                    "Owned OCI client remains; outer recovery required"
                ) from None
        # This runtime permits one active guest. New private-namespace processes
        # must disappear too; an empty state list or dead CLI is insufficient.
        if record.get("before") is not None:
            end = min(self.deadline, time.monotonic() + 4)
            while True:
                remaining = {
                    pid: born
                    for pid, born in processes().items()
                    if record["before"].get(pid) != born
                }
                if not remaining:
                    break
                if time.monotonic() >= end:
                    raise SandboxUnavailable("Runtime processes remain; outer recovery required")
                time.sleep(0.05)
        record["closed"] = True
        return {"id": label, "stopped": True, "removed": True}
