"""Fixed startup isolation checks under the original whole-task supervision."""

import hashlib
import socket
import time
from contextlib import contextmanager
from copy import deepcopy

from judge.container_contract import PYTHON, WORKLOAD_PROFILE, resource_limits
from judge.container_isolation_protocol import (
    CHECKS,
    SCOPE,
    require,
    target_identity,
    validate_details,
    validate_report,
    validate_request,
    verify_identity,
)
from judge.container_resources import GROUP, counters, host_pid_probe
from judge.gpu_session import GPUExecutionError
from judge.sandbox import SandboxUnavailable


class CancelProbe(Exception):
    pass


def check_resources(runtime, request):
    validate_request(request)
    target, selection = getattr(runtime, "target", None), getattr(runtime, "device_spec", None)
    target_identity(target, selection)
    require(
        runtime.resource_profile == WORKLOAD_PROFILE, "Resource probe requires its fixed budget"
    )
    runtime.tick()
    report = {
        "scope": SCOPE,
        "gpu": None if target is None else target["uuid"],
        "canary_sha256": hashlib.sha256(request["canary"].encode()).hexdigest(),
        "resource_profile": WORKLOAD_PROFILE,
        "limits": resource_limits(WORKLOAD_PROFILE),
        "checks": [],
        "cleanup": [],
    }
    protected = runtime.base / "resource-private"
    with protected.open("x") as stream:
        stream.write("fixed controller-private infrastructure canary")

    @contextmanager
    def session(label):
        runtime.create(label, PYTHON, ["-I", "/opt/praximesh/resource_guest.py"])
        original = runtime.containers[label]
        with runtime.session(label, mode="run", on_tick=runtime.tick) as active:
            yield active
        require(
            runtime.containers[label] is original and original.get("closed") is True,
            "Resource probe original guest was not cleaned",
        )
        report["cleanup"].append(deepcopy(original.get("cleanup_evidence")))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        identity_request = {
            "operation": "identity",
            "canary": request["canary"],
            "protected_path": str(protected),
            "port": listener.getsockname()[1],
            "gpu": target is not None,
        }

        def identity(active):
            value, _elapsed = active.exchange(identity_request, seconds=10)
            verify_identity(value, request, target, selection)
            return value

        def observe(active, label):
            original = runtime.containers[label]
            require(original["process"] is active.process, "Resource attach identity changed")
            state = runtime.inspect(label)
            require(
                state["status"] == "running" and active.process.poll() is None,
                "Resource guest stopped before interruption",
            )
            return {
                "scope": "runsc-guest-v1",
                "id": label,
                "status": "running",
                "attach_alive": True,
            }

        for name in CHECKS:
            runtime.tick()
            details = {}
            row = {"name": name, "passed": False, "cpu_recovery": False, "details": details}
            report["checks"].append(row)
            if name == "pid-limit":
                initial = int((GROUP / "pids.current").read_text())
                details.update(host_pid_probe(), initial_tasks=initial)
                runtime.tick()
            else:
                with session(name) as active:
                    value = identity(active)
                    if name == "identity":
                        details["guest"] = value
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
                                {"operation": operation},
                                seconds=15 if name == "memory-limit" else 1,
                            )
                        except GPUExecutionError as error:
                            require(error.verdict == expected, "Unexpected resource limit outcome")
                            details["verdict"] = error.verdict
                            if name == "memory-limit":
                                details["oom_kills"] = (
                                    counters("memory.events")["oom_kill"] - before
                                )
                        else:
                            raise SandboxUnavailable(
                                "Resource limit failed to stop the fixed guest"
                            )
                    elif name in {"cancel", "client-exit"}:
                        value, _elapsed = active.exchange({"operation": "started-hang"}, seconds=2)
                        require(
                            value == {"event": "busy"}, "Resource guest did not acknowledge work"
                        )
                        if name == "client-exit":
                            details["observation"] = observe(active, name)
                            active.process.kill()
                            active.process.wait(timeout=2)
                        else:
                            until = time.monotonic() + 0.5

                            def cancel(until=until, active=active, name=name, details=details):
                                runtime.tick()
                                if time.monotonic() >= until:
                                    details["observation"] = observe(active, name)
                                    raise CancelProbe

                            active.on_tick = cancel
                            try:
                                active.exchange({"operation": "hang"}, seconds=3)
                            except CancelProbe:
                                pass
                            else:
                                raise SandboxUnavailable("Resource probe ignored cancellation")
                        details["interruption"] = name
                    elif name == "cpu-limit":
                        before = counters("cpu.stat")
                        started = time.monotonic()
                        value, _elapsed = active.exchange({"operation": "busy"}, seconds=6)
                        elapsed = time.monotonic() - started
                        after = counters("cpu.stat")
                        require(
                            value == {"event": "busy-finished"}, "Fixed CPU work did not finish"
                        )
                        details.update(
                            elapsed_seconds=elapsed,
                            cpu_seconds=(after["usage_usec"] - before["usage_usec"]) / 1e6,
                            throttled_periods=after["nr_throttled"] - before["nr_throttled"],
                        )
                        active.finish()
                    elif name == "storage-limit":
                        value, _elapsed = active.exchange(
                            {"operation": "temporary-storage"}, seconds=5
                        )
                        require(type(value) is dict, "Fixed storage check returned invalid data")
                        details.update(value)
                        active.finish()
            validate_details(name, details, request, target, selection)
            runtime.tick()
            with session(name + "-recovery") as recovered:
                identity(recovered)
                recovered.finish()
            row.update(passed=True, cpu_recovery=True)
    runtime.tick()
    validate_report(report, request, target=target, selection=selection)
    return report
