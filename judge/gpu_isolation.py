"""Actual resource checks shared by GPU startup and explicit CPU validation."""

import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

from judge.gpu_command import run_command
from judge.gpu_session import GPUExecutionError, GPUSession
from judge.sandbox import SandboxUnavailable

CHECKS = (
    "identity",
    "output",
    "timeout",
    "cancel",
    "client-exit",
    "cpu-limit",
    "storage-limit",
    "memory-limit",
    "pid-limit",
)
CONTAINERS = tuple(
    label
    for name in CHECKS
    for label in ((name + "-recovery",) if name == "pid-limit" else (name, name + "-recovery"))
)


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def request_values(request):
    if (
        type(request) is not dict
        or set(request) != {"canary", "port"}
        or type(request["canary"]) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", request["canary"])
        or type(request["port"]) is not int
        or not 1 <= request["port"] <= 65535
    ):
        raise ValueError("A fresh infrastructure challenge and host listener are required")
    return request


def counters(filename):
    return {
        key: int(value)
        for key, value in (
            line.split()
            for line in (Path("/sys/fs/cgroup/workloads") / filename).read_text().splitlines()
        )
    }


def verify_identity(value, request, target):
    require(
        type(value) is dict
        and set(value)
        == {
            "event",
            "uid",
            "root_write",
            "private_file_visible",
            "host_loopback_reachable",
            "gpu_nodes",
            "devices",
            "capabilities",
            "environment_names",
            "sha256",
        }
        and value["event"] == "identity"
        and type(value["uid"]) is int
        and value["uid"] == 65534
        and all(
            value[key] is False
            for key in ("root_write", "private_file_visible", "host_loopback_reachable")
        )
        and value["sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest(),
        "Resource probe did not demonstrate process and data isolation",
    )
    require(
        type(value["capabilities"]) is str
        and re.fullmatch(r"0+", value["capabilities"])
        and type(value["environment_names"]) is list
        and all(type(name) is str for name in value["environment_names"])
        and not {
            "DATABASE_URL",
            "APP_SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "CONTAINER_HOST",
            "DOCKER_HOST",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        }.intersection(value["environment_names"]),
        "Resource probe inherited capabilities or private configuration",
    )
    nodes = sorted(["nvidiactl", "nvidia-uvm", f"nvidia{target['index']}"]) if target else []
    devices = [[target["uuid"], target["model"], target["driver"]]] if target else []
    require(
        value["gpu_nodes"] == nodes and value["devices"] == devices,
        "Resource probe can see an unexpected device set",
    )


def validate_report(report, request, target):
    require(
        type(report) is dict
        and set(report) == {"gpu", "canary_sha256", "checks", "cleanup"}
        and report["gpu"] == (target["uuid"] if target else None)
        and report["canary_sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest()
        and type(report["checks"]) is list
        and len(report["checks"]) == len(CHECKS)
        and type(report["cleanup"]) is list
        and len(report["cleanup"]) == len(CONTAINERS),
        "Incomplete resource check report",
    )
    for row, name in zip(report["checks"], CHECKS, strict=True):
        require(
            type(row) is dict
            and set(row) == {"name", "passed", "cpu_recovery", "details"}
            and row["name"] == name
            and row["passed"] is True
            and row["cpu_recovery"] is True
            and type(row["details"]) is dict,
            "Missing or failed resource check",
        )
    names = CONTAINERS
    for row, name in zip(report["cleanup"], names, strict=True):
        require(
            type(row) is dict
            and set(row) == {"name", "id", "running_before_cleanup", "exit_code", "stopped"}
            and row["name"] == name
            and row["stopped"] is True
            and type(row["id"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", row["id"])
            and type(row["running_before_cleanup"]) is bool
            and type(row["exit_code"]) is int,
            "Resource container cleanup was not verified",
        )
    require(
        len({row["id"] for row in report["cleanup"]}) == len(names),
        "Resource checks reused a prior container",
    )


class CancelProbe(Exception):
    pass


def check_resources(runtime, request, *, expect_gpu, record):
    request_values(request)
    require(
        type(expect_gpu) is bool and expect_gpu == (runtime.target is not None),
        "Resource check device mode must be explicit",
    )
    record.update(
        gpu=runtime.target["uuid"] if expect_gpu else None,
        canary_sha256=hashlib.sha256(request["canary"].encode()).hexdigest(),
        checks=[],
        cleanup=[],
    )
    fixture = runtime.base / "resource-guest.py"
    shutil.copyfile(Path(__file__).with_name("gpu_isolation_guest.py"), fixture)
    fixture.chmod(0o444)
    protected = runtime.base / "protected-canary"
    protected.write_text("fixed controller-private infrastructure canary")
    identity_request = dict(
        request, operation="identity", protected_path=str(protected), gpu=expect_gpu
    )

    def session(label):
        runtime.create(
            label,
            "/usr/local/bin/python",
            ["-I", "/opt/fixture.py"],
            mounts=[(fixture, "/opt/fixture.py")],
        )
        initial_ooms = counters("memory.events")["oom_kill"]

        def cleanup():
            record["cleanup"].append(dict(name=label, **runtime.stop_container(label)))

        def exit_verdict(_code):
            return (
                "memory_limit"
                if (
                    not runtime.inspect(label)["State"]["Running"]
                    and counters("memory.events")["oom_kill"] > initial_ooms
                )
                else None
            )

        return GPUSession(
            runtime.attach(label),
            cleanup,
            architecture=[],
            mode="run",
            on_tick=runtime.tick,
            exit_verdict=exit_verdict,
        )

    def identity(active):
        values, _elapsed = active.exchange(identity_request, seconds=10)
        verify_identity(values, request, runtime.target)
        return values

    for name in CHECKS:
        details = {}
        row = {"name": name, "passed": False, "cpu_recovery": False, "details": details}
        record["checks"].append(row)
        if name == "pid-limit":
            # Guest tasks are virtualized; measure the declared host cgroup
            # ceiling with fixed trusted native processes after guests stopped.
            before = counters("pids.events")["max"]
            helper = runtime.base / "host-pids.py"
            shutil.copyfile(Path(__file__).with_name("gpu_host_pids.py"), helper)
            output = run_command(
                [sys.executable, "-I", str(helper)],
                env=runtime.env,
                deadline=runtime.deadline,
                on_tick=runtime.tick,
                seconds=10,
                max_output=1024,
            )
            values = json.loads(output.stdout)
            events = counters("pids.events")["max"] - before
            remaining = int(Path("/sys/fs/cgroup/workloads/pids.current").read_text())
            require(
                type(values) is dict
                and set(values) == {"event", "limited", "children"}
                and values["event"] == "host-pids"
                and values["limited"] is True
                and type(values["children"]) is int
                and values["children"] == 255
                and events > 0
                and remaining == 0,
                "Host PID ceiling or complete native probe cleanup was not verified",
            )
            details.update(values, denied_forks=events, remaining=remaining)
            with session(name + "-recovery") as recovered:
                identity(recovered)
                recovered.finish()
            row.update(passed=True, cpu_recovery=True)
            continue
        with session(name) as active:
            values = identity(active)
            if name == "identity":
                details["guest"] = values
                active.finish()
            elif name in {"output", "timeout", "memory-limit"}:
                operation, expected = {
                    "output": ("output", "output_limit"),
                    "timeout": ("hang", "time_limit"),
                    "memory-limit": ("memory", "memory_limit"),
                }[name]
                before = counters("memory.events")["oom_kill"]
                try:
                    active.exchange(
                        {"operation": operation}, seconds=10 if name == "memory-limit" else 1
                    )
                except GPUExecutionError as error:
                    require(error.verdict == expected, "Resource failure returned a wrong outcome")
                    if name == "memory-limit":
                        require(
                            counters("memory.events")["oom_kill"] > before,
                            "Memory probe did not cause a verified cgroup OOM",
                        )
                    details["verdict"] = error.verdict
                else:
                    raise SandboxUnavailable("Resource limit did not stop the fixed guest")
            elif name == "cancel":
                until = time.monotonic() + 0.5

                def cancel(until=until):
                    runtime.tick()
                    if time.monotonic() >= until:
                        raise CancelProbe

                active.on_tick = cancel
                try:
                    active.exchange({"operation": "hang"}, seconds=3)
                except CancelProbe:
                    details["cancelled"] = True
                else:
                    raise SandboxUnavailable("Resource probe ignored cancellation")
            elif name == "client-exit":
                values, _elapsed = active.exchange({"operation": "started-hang"}, seconds=2)
                require(values == {"event": "busy"}, "Resource guest did not start")
                active.process.kill()
                active.process.wait(timeout=2)
                require(
                    runtime.inspect(name)["State"]["Running"],
                    "Client exit probe did not leave a live container to recover",
                )
                details["container_outlived_client"] = True
            elif name == "cpu-limit":
                before = counters("cpu.stat")
                started = time.monotonic()
                values, _elapsed = active.exchange({"operation": "busy"}, seconds=6)
                elapsed = time.monotonic() - started
                after = counters("cpu.stat")
                cpu = (after["usage_usec"] - before["usage_usec"]) / 1e6
                periods = after["nr_throttled"] - before["nr_throttled"]
                require(
                    values == {"event": "busy-finished"}
                    and elapsed >= 3
                    and cpu >= 1
                    and cpu / elapsed <= 0.65
                    and periods > 0,
                    "Resource CPU quota did not actually throttle the guest",
                )
                details.update(elapsed_seconds=elapsed, cpu_seconds=cpu, throttled_periods=periods)
                active.finish()
            elif name == "storage-limit":
                values, _elapsed = active.exchange({"operation": "temporary-storage"}, seconds=5)
                require(
                    values.get("event") == "storage"
                    and values.get("limited") is True
                    and type(values.get("mib")) is int
                    and 0 < values["mib"] <= 32,
                    "Resource temporary storage is not bounded",
                )
                details.update(values)
                active.finish()
        with session(name + "-recovery") as recovered:
            identity(recovered)
            recovered.finish()
        row.update(passed=True, cpu_recovery=True)
    validate_report(record, request, runtime.target)
    return record
