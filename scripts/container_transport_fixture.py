"""Fixed CPU controller for real container IPC checks; no arbitrary code input."""

import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.container_transport import SOCKET, ContainerListener  # noqa: E402
from judge.gpu_session import GPUSession  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402


def save(report):
    path = Path(STATE) / "transport.tmp"
    path.write_text(json.dumps(report))
    path.replace(Path(STATE) / "transport.json")


def main():
    mode = sys.argv[1]
    if mode not in {
        "normal",
        "disconnect",
        "client-exit",
        "cancel",
        "trailing",
        "wrong-start",
        "oversized",
        "deadline",
    }:
        raise ValueError("Unknown fixed transport scenario")
    report = {"mode": mode, "guest_started": False, "cleanup": [], "gpu_executed": False}
    channel = listener = None
    try:
        listener = ContainerListener(on_tick=lambda: None)
        channel = listener.accept()
        request = channel.receive()
        if (
            type(request) is not dict
            or set(request) != {"event", "canary", "padding"}
            or request["event"] != "probe"
            or type(request["canary"]) is not str
            or not re.fullmatch("[a-f0-9]{64}", request["canary"])
            or request["padding"] != "x" * 1024**2
        ):
            raise SandboxUnavailable("Unexpected fixed CPU probe request")
        runtime = ContainerRuntime(deadline=listener.status["deadline"] - 3, on_tick=lambda: None)
        runtime.create("control", PYTHON, ["-I", "/opt/praximesh/cpu_guest.py"])

        def cleanup():
            report["cleanup"].append(runtime.stop_container("control"))

        def tick():
            runtime.tick()
            channel.connected()

        with GPUSession(
            runtime.attach("control"), cleanup, architecture=[], mode="submit", on_tick=tick
        ) as active:
            report["guest_started"] = True
            value, _ = active.exchange(
                {
                    "operation": "identity",
                    "protected_path": SOCKET,
                    "port": 9,
                    "canary": request["canary"],
                },
                seconds=5,
            )
            assert value["private_file_visible"] is False and value["gpu_nodes"] == []
            assert value["sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest()
            report["identity"] = value
            channel.send({"event": "guest-ready", "identity": value, "padding": request["padding"]})
            del request
            command = channel.receive()
            if mode == "normal":
                if command != {"event": "finish"}:
                    raise SandboxUnavailable("Unexpected completion request")
                active.finish()
            else:
                if command != {"event": "hang"}:
                    raise SandboxUnavailable("Unexpected fault request")
                value, _ = active.exchange({"operation": "started-hang"}, seconds=5)
                assert value == {"event": "busy"}
                channel.send({"event": "busy"})
                while True:
                    if mode != "deadline":
                        tick()
                    time.sleep(0.05)
        report["completed"] = True
        channel.send({"event": "completed", "recovered": True})
    except SandboxUnavailable as error:
        report["failure"] = str(error)
    except Exception as error:
        report["unexpected"] = type(error).__name__ + ": " + str(error)
    finally:
        if channel is not None:
            channel.close()
        if listener is not None:
            listener.close()
        save(report)
    # Leave the fixed report for the host observer; PID 1 still limits lifetime.
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
