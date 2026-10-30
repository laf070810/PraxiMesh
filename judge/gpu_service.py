"""Owned task services with a deadline independent of the GPU worker process.

Only trusted controllers supply the executable and arguments. Learner source
must stay in the isolated guest protocol, never in a service command or journal.
This component neither creates a GPU sandbox nor authorizes GPU submissions.
"""

import hashlib
import math
import os
import re
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable

START_SECONDS = 5
STOP_SECONDS = 5
RESERVE_SECONDS = START_SECONDS + STOP_SECONDS
MAX_SLOT_SECONDS = 180
CONTROL_SECONDS = 2
PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "Transient",
    "Type",
    "Restart",
    "KillMode",
    "KillSignal",
    "FinalKillSignal",
    "SendSIGKILL",
    "TimeoutStartUSec",
    "TimeoutStopUSec",
    "RuntimeMaxUSec",
    "RuntimeRandomizedExtraUSec",
    "ControlGroup",
    "InvocationID",
    "MainPID",
    "Delegate",
    "CPUQuotaPerSecUSec",
    "MemoryMax",
    "MemorySwapMax",
    "TasksMax",
    "StandardOutput",
    "StandardError",
)


def duration_seconds(value):
    """Parse systemctl's finite timespan output, rejecting unknown/unbounded units."""
    if value == "0":
        return 0.0
    pieces = re.findall(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h|d)(?: |$)", value)
    if not pieces or " ".join(number + unit for number, unit in pieces) != value:
        raise SandboxUnavailable("Unknown task service time limit")
    scale = {
        "us": Decimal("0.000001"),
        "ms": Decimal("0.001"),
        "s": 1,
        "min": 60,
        "h": 3600,
        "d": 86400,
    }
    return float(sum(Decimal(number) * scale[unit] for number, unit in pieces))


class OwnedJobService:
    def __init__(self, guard, job_id, reservation_token, *, purpose="task"):
        if type(guard) is not DeviceLock:
            raise ValueError("An owned GPU device lock is required")
        guard.assert_held()
        if purpose not in {"task", "recovery"}:
            raise ValueError("Unknown owned service purpose")
        self.guard = guard
        self.purpose = purpose
        self.job_id = uuid.UUID(str(job_id))
        self.reservation_token = uuid.UUID(str(reservation_token))
        identity = f"v1:{os.getuid()}:{guard.device_uuid}:{self.job_id}:{self.reservation_token}"
        if purpose == "recovery":
            identity += ":recovery"
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.unit = (
            "praximesh-gpu-"
            + ("job" if purpose == "task" else purpose)
            + "-"
            + digest[:40]
            + ".service"
        )
        self.description = "PraxiMesh GPU " + purpose + " v1 " + digest
        self.cgroup_name = (
            f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{self.unit}"
        )
        self.cgroup = Path("/sys/fs/cgroup") / self.cgroup_name.lstrip("/")
        self.invocation = None
        self.runtime_limit = None

    def _command(self, program, *args):
        self.guard.assert_held()
        try:
            result = subprocess.run(
                [program, *args],
                capture_output=True,
                text=True,
                env=dict(os.environ, LC_ALL="C"),
                timeout=CONTROL_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxUnavailable("Task service manager did not respond in time") from None
        self.guard.assert_held()
        if len(result.stdout) > 65536 or len(result.stderr) > 65536:
            raise SandboxUnavailable("Task service manager returned excessive output")
        return result

    def inspect(self):
        result = self._command(
            "systemctl",
            "--user",
            "show",
            "--no-pager",
            "--property=" + ",".join(PROPERTIES),
            self.unit,
        )
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in PROPERTIES or key in values:
                raise SandboxUnavailable("Invalid task service inspection")
            values[key] = value
        if values.get("LoadState") == "not-found" and values.get("Id") == self.unit:
            return None
        if result.returncode != 0 or set(values) != set(PROPERTIES):
            raise SandboxUnavailable("Task service state could not be verified")
        expected = {
            "Id": self.unit,
            "Description": self.description,
            "Transient": "yes",
            "Type": "exec",
            "Restart": "no",
            "KillMode": "control-group",
            "KillSignal": "9",
            "FinalKillSignal": "9",
            "SendSIGKILL": "yes",
            "Delegate": "yes",
            "MemoryMax": str(2 * 1024**3),
            "MemorySwapMax": "0",
            "TasksMax": "512",
            "StandardOutput": "null",
            "StandardError": "null",
        }
        if any(values[key] != value for key, value in expected.items()):
            raise SandboxUnavailable("Task service ownership or containment does not match")
        limits = {
            "TimeoutStartUSec": START_SECONDS,
            "TimeoutStopUSec": STOP_SECONDS,
            "RuntimeRandomizedExtraUSec": 0,
            "CPUQuotaPerSecUSec": 2,
        }
        if any(duration_seconds(values[key]) != value for key, value in limits.items()):
            raise SandboxUnavailable("Task service limits changed")
        runtime = duration_seconds(values["RuntimeMaxUSec"])
        maximum = self.runtime_limit or (MAX_SLOT_SECONDS - RESERVE_SECONDS)
        if not 0 < runtime <= maximum + 0.000001:
            raise SandboxUnavailable("Task service has no valid independent deadline")
        invocation = values["InvocationID"]
        if not re.fullmatch(r"[a-f0-9]{32}", invocation):
            raise SandboxUnavailable("Task service invocation is missing")
        if self.invocation is not None and invocation != self.invocation:
            raise SandboxUnavailable("Task service invocation changed")
        self.invocation = invocation
        if values["ControlGroup"] not in {"", self.cgroup_name}:
            raise SandboxUnavailable("Task service cgroup does not match its owner")
        if values["ActiveState"] not in {"inactive", "failed"} and not values["ControlGroup"]:
            raise SandboxUnavailable("Active task service has no verified cgroup")
        return values

    def populated(self):
        self.guard.assert_held()
        try:
            entries = dict(
                line.split() for line in (self.cgroup / "cgroup.events").read_text().splitlines()
            )
        except FileNotFoundError:
            if self.cgroup.exists():
                raise SandboxUnavailable("Task cgroup population could not be verified") from None
            return False
        except (OSError, ValueError):
            raise SandboxUnavailable("Task cgroup population could not be verified") from None
        if entries.get("populated") not in {"0", "1"}:
            raise SandboxUnavailable("Task cgroup population is unknown")
        return entries["populated"] == "1"

    def limits(self):
        """Read actual parent cgroup files, not just accepted service properties."""
        self.guard.assert_held()
        try:
            values = {
                name: (self.cgroup / name).read_text().strip()
                for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max")
            }
            quota, period = map(int, values["cpu.max"].split())
        except (OSError, ValueError):
            raise SandboxUnavailable("Task parent cgroup limits could not be verified") from None
        if (
            period <= 0
            or quota != 2 * period
            or values["memory.max"] != str(2 * 1024**3)
            or values["memory.swap.max"] != "0"
            or values["pids.max"] != "512"
        ):
            raise SandboxUnavailable("Task parent cgroup limits were not applied")
        return values

    def start(self, command, *, deadline, on_tick):
        """Launch an absolute, trusted controller command; never a learner program."""
        self.guard.assert_held()
        starting = time.monotonic()
        remaining = deadline - starting
        if not math.isfinite(remaining) or not RESERVE_SECONDS < remaining <= MAX_SLOT_SECONDS:
            raise SandboxUnavailable("Insufficient task reservation for startup and cleanup")
        if (
            type(command) not in {tuple, list}
            or not command
            or any(type(part) is not str or not part or "\0" in part for part in command)
            or not Path(command[0]).is_absolute()
        ):
            raise ValueError("A fixed trusted controller command is required")
        if self.inspect() is not None or self.populated():
            raise SandboxUnavailable("Task service already exists; recovery is required first")
        on_tick()
        self.guard.assert_held()
        # Recompute after the callback; a lock wait cannot buy a new reservation.
        starting = time.monotonic()
        remaining = deadline - starting
        if remaining <= RESERVE_SECONDS:
            raise SandboxUnavailable("Task reservation expired before service startup")
        self.runtime_limit = math.floor((remaining - RESERVE_SECONDS) * 1e6) / 1e6
        if self.runtime_limit <= 0:
            raise SandboxUnavailable("Task reservation is too short for a finite service timer")
        properties = [
            "Type=exec",
            "Restart=no",
            "Slice=app.slice",
            "Delegate=yes",
            "UMask=0077",
            "CPUQuota=200%",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            "TasksMax=512",
            "KillMode=control-group",
            "KillSignal=SIGKILL",
            "FinalKillSignal=SIGKILL",
            "SendSIGKILL=yes",
            "TimeoutStartFailureMode=kill",
            "TimeoutStopFailureMode=kill",
            f"TimeoutStartSec={START_SECONDS}",
            f"TimeoutStopSec={STOP_SECONDS}",
            f"RuntimeMaxSec={self.runtime_limit:.6f}",
            "RuntimeRandomizedExtraSec=0",
            "StandardOutput=null",
            "StandardError=null",
        ]
        result = self._command(
            "systemd-run",
            "--user",
            "--quiet",
            "--no-block",
            "--collect",
            "--unit=" + self.unit,
            "--description=" + self.description,
            *("--property=" + value for value in properties),
            "--",
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/gpu_service_entry.py"),
            "--start-before",
            str(starting + START_SECONDS),
            "--",
            *command,
        )
        if result.returncode:
            raise SandboxUnavailable("Task service startup failed; owned recovery is required")
        startup_deadline = min(deadline - STOP_SECONDS, time.monotonic() + START_SECONDS)
        while time.monotonic() < startup_deadline:
            state = self.inspect()
            if state and state["ActiveState"] == "active" and self.populated():
                self.limits()
                return state
            if state is None or state["ActiveState"] in {"inactive", "failed"}:
                raise SandboxUnavailable("Task controller exited before becoming ready")
            on_tick()
            time.sleep(0.05)
        raise SandboxUnavailable("Task controller did not start within its reservation")

    def stop(self):
        """Stop only the matching owned invocation and verify its entire cgroup."""
        state = self.inspect()
        stop_failed = False
        if state is not None and state["ActiveState"] not in {"inactive", "failed"}:
            result = self._command("systemctl", "--user", "stop", "--no-block", self.unit)
            # A naturally exiting --collect unit can disappear between show
            # and stop. Recheck identity and the entire cgroup even when the
            # command fails; its return code alone proves neither outcome.
            stop_failed = result.returncode != 0
        deadline = time.monotonic() + STOP_SECONDS + CONTROL_SECONDS
        while True:
            state = self.inspect()
            stopped = state is None or (
                state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0"
            )
            if stopped and not self.populated():
                return {
                    "unit": self.unit,
                    "invocation": self.invocation,
                    "stopped": True,
                    "cgroup_empty": True,
                }
            if stop_failed:
                raise SandboxUnavailable("Owned task service could not be stopped")
            if time.monotonic() >= deadline:
                raise SandboxUnavailable("Owned task service still has running processes")
            time.sleep(0.05)
