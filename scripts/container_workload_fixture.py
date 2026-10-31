"""Fixed CPU resource probes in the trusted outer container; never a GPU worker."""

import hashlib
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import STATE, WORKLOAD_PROFILE, resource_limits  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.container_resources import counters, host_pid_probe  # noqa: E402
from judge.container_transport import ContainerListener  # noqa: E402
from judge.gpu_session import GPUExecutionError  # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else None
    if mode not in {"cpu", "storage", "memory", "pids"}:
        raise ValueError("Unknown fixed resource probe")
    listener = ContainerListener(on_tick=lambda: None)
    channel = None
    cleanup = []
    try:
        channel = listener.accept()
        if channel.receive() != {"event": "prepare-resource", "name": mode}:
            raise ValueError("Unexpected fixed resource request")
        runtime = ContainerRuntime(deadline=listener.status["deadline"] - 3, on_tick=lambda: None)
        if runtime.resource_profile != WORKLOAD_PROFILE:
            raise ValueError("Resource probes require the explicit task budget")
        ready = {
            "event": "resource-ready",
            "name": mode,
            "resource_profile": WORKLOAD_PROFILE,
            "limits": resource_limits(WORKLOAD_PROFILE),
        }

        def begin():
            channel.send(ready)
            if channel.receive() != {"event": "start"}:
                raise ValueError("Unexpected resource start acknowledgement")

        def tick():
            runtime.tick()
            channel.connected()

        if mode == "pids":
            begin()
            details = host_pid_probe()
        else:
            with socket.socket() as local:
                local.bind(("127.0.0.1", 0))
                local.listen()
                protected = Path(STATE) / "resource-private"
                protected.write_text("fixed private resource canary")
                canary = os.urandom(32).hex()
                runtime.create(
                    "resource", "/usr/local/bin/python", ["-I", "/opt/praximesh/cpu_guest.py"]
                )
                with runtime.session(
                    "resource", mode="submit", on_tick=tick, on_cleanup=cleanup.append
                ) as session:
                    identity, _ = session.exchange(
                        {
                            "operation": "identity",
                            "protected_path": str(protected),
                            "port": local.getsockname()[1],
                            "canary": canary,
                        },
                        seconds=8,
                    )
                    assert identity == {
                        "event": "identity",
                        "uid": 0,
                        "root_write": False,
                        "private_file_visible": False,
                        "host_loopback_reachable": False,
                        "gpu_nodes": [],
                        "capabilities": "0000000000000000",
                        "environment_names": identity["environment_names"],
                        "sha256": hashlib.sha256(canary.encode()).hexdigest(),
                    }, identity
                    assert set(identity["environment_names"]) <= {
                        "PATH",
                        "PYTHONDONTWRITEBYTECODE",
                        "LC_CTYPE",
                        "HOME",
                        "LANG",
                    }
                    ready["guest"] = identity
                    begin()
                    if mode == "cpu":
                        before = counters("cpu.stat")
                        started = time.monotonic()
                        value, _ = session.exchange({"operation": "busy"}, seconds=8)
                        elapsed = time.monotonic() - started
                        after = counters("cpu.stat")
                        cpu = (after["usage_usec"] - before["usage_usec"]) / 1e6
                        periods = after["nr_throttled"] - before["nr_throttled"]
                        assert value == {"event": "busy-finished"}
                        assert elapsed >= 3 and cpu >= 0.5 and cpu / elapsed <= 0.7 and periods > 0
                        details = {
                            "elapsed_seconds": elapsed,
                            "cpu_seconds": cpu,
                            "throttled_periods": periods,
                        }
                    elif mode == "storage":
                        value, _ = session.exchange({"operation": "temporary-storage"}, seconds=8)
                        assert value["event"] == "storage" and value["limited"] is True
                        assert 1 <= value["mib"] <= 32
                        details = value
                    else:
                        before = counters("memory.events")["oom_kill"]
                        try:
                            session.exchange({"operation": "memory"}, seconds=15)
                        except GPUExecutionError as error:
                            assert error.verdict == "memory_limit", error.verdict
                        else:
                            raise RuntimeError("Fixed memory allocation was not limited")
                        kills = counters("memory.events")["oom_kill"] - before
                        assert kills > 0
                        details = {"oom_kills": kills, "classification": "memory_limit"}
                    if mode != "memory":
                        session.finish()
            assert cleanup == [{"id": "resource", "stopped": True, "removed": True}]
        channel.send(
            {
                "event": "resource-checked",
                "name": mode,
                "passed": True,
                "ready": False,
                "gpu_executed": False,
                "resource_profile": WORKLOAD_PROFILE,
                "details": details,
                "cleanup": cleanup,
            }
        )
        if channel.receive() != {"event": "acknowledged"}:
            raise ValueError("Unexpected resource completion acknowledgement")
    except Exception as error:
        # This fixture accepts no learner code; retain its bounded failure evidence.
        failure = {
            "event": "resource-failed",
            "name": mode,
            "failure_kind": type(error).__name__,
            "failure": str(error)[:1024],
            "cleanup": cleanup,
        }
        try:
            failure["memory_events"] = counters("memory.events")
            if channel is not None:
                channel.send(failure)
                channel.receive()
        except Exception:
            pass
        raise
    finally:
        if channel is not None:
            channel.close()
        listener.close()


if __name__ == "__main__":
    main()
