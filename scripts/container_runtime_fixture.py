"""Fixed CPU-only checks inside the supervised runsc infrastructure container."""

import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import STATE  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.gpu_session import GPUExecutionError, GPUSession  # noqa: E402


def save(name, value):
    path = Path(STATE) / (name + ".tmp")
    path.write_text(json.dumps(value))
    path.replace(Path(STATE) / (name + ".json"))


def main():
    mode = sys.argv[1]
    if mode not in {"checks", "deadline", "orphan"}:
        raise ValueError("Unknown fixed CPU scenario")
    end = time.monotonic() + 3
    while True:
        try:
            status = json.loads((Path(STATE) / "status.json").read_text())
            break
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= end:
                raise RuntimeError("Supervisor did not publish its bounded readiness") from None
            time.sleep(0.02)
    report = {"passed": False, "gpu_executed": False, "checks": [], "cleanup": []}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    protected = Path(STATE) / "controller-private.txt"
    protected.write_text("fixed private controller canary")
    request = {
        "operation": "identity",
        "protected_path": str(protected),
        "port": listener.getsockname()[1],
        "canary": os.urandom(32).hex(),
    }
    runtime = None

    def identity(session):
        value, _elapsed = session.exchange(request, seconds=8)
        report["identity"] = value
        assert value == {
            "event": "identity",
            "uid": 0,
            "root_write": False,
            "private_file_visible": False,
            "host_loopback_reachable": False,
            "gpu_nodes": [],
            "capabilities": "0000000000000000",
            "environment_names": value["environment_names"],
            "sha256": hashlib.sha256(request["canary"].encode()).hexdigest(),
        }, value
        assert set(value["environment_names"]) <= {
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "LC_CTYPE",
            "HOME",
            "LANG",
        }, value["environment_names"]
        return value

    def session(label):
        runtime.create(label, "/usr/local/bin/python", ["-I", "/opt/praximesh/cpu_guest.py"])

        def cleanup():
            report["cleanup"].append(runtime.stop_container(label))

        return GPUSession(
            runtime.attach(label), cleanup, architecture=[], mode="submit", on_tick=runtime.tick
        )

    try:
        runtime = ContainerRuntime(
            deadline=status["deadline"] - 3, on_tick=lambda: None, diagnostics=True
        )
        with session("identity") as active:
            value = identity(active)
            state = runtime.inspect("identity")
            save("runtime-ready", {"state": state, "identity": value})
            while not (Path(STATE) / "observe").exists():
                runtime.tick()
                time.sleep(0.05)
            if mode in {"deadline", "orphan"}:
                value, _elapsed = active.exchange({"operation": "started-hang"}, seconds=5)
                assert value == {"event": "busy"}
                save("runtime-busy", {"state": runtime.inspect("identity")})
                if mode == "orphan":
                    time.sleep(2)
                    os._exit(0)
                while True:
                    time.sleep(0.1)  # The independent supervisor must stop us.
            active.finish()
        report["checks"].append({"name": "identity", "passed": True})
        for name in ("output", "timeout", "temporary-storage", "client-exit"):
            with session(name) as active:
                identity(active)
                runtime.inspect(name)
                if name in {"output", "timeout"}:
                    expected = "output_limit" if name == "output" else "time_limit"
                    try:
                        active.exchange(
                            {"operation": "output" if name == "output" else "hang"}, seconds=1
                        )
                    except GPUExecutionError as error:
                        assert error.verdict == expected, error.verdict
                    else:
                        raise RuntimeError("Expected bounded session failure")
                elif name == "temporary-storage":
                    value, _elapsed = active.exchange({"operation": name}, seconds=5)
                    assert value["limited"] is True and 24 <= value["mib"] <= 32, value
                    active.finish()
                else:
                    active.process.kill()
                    active.process.wait(timeout=3)
                    # Client exit alone must never stand in for this cleanup.
                    state = runtime.inspect(name)
                    report["client_exit_guest_status"] = state["status"]
            with session(name + "-recovery") as active:
                identity(active)
                active.finish()
            report["checks"].append({"name": name, "passed": True, "fresh_recovery": True})
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        if error.__context__ is not None:
            report["preceding_failure"] = (
                type(error.__context__).__name__ + ": " + str(error.__context__)
            )
        log = Path(STATE) / "oci/runtime.log"
        if log.is_file():
            report["runtime_log"] = log.read_text()[-24000:]
        if getattr(error, "diagnostics", None):
            report["diagnostics"] = error.diagnostics.decode(errors="replace")[-4096:]
    finally:
        save("runtime", report)
        listener.close()
    # Give the host observer time to collect the bounded report before it stops
    # the whole container and verifies every recorded host process disappeared.
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
