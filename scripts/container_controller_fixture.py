"""Fixed CPU lifecycle fixture inside the trusted control image, never a GPU worker."""

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON  # noqa: E402
from judge.container_controller import STATE, ContainerController  # noqa: E402
from judge.container_journal import PersistentRuntimeService  # noqa: E402


def save(name, value):
    path = STATE / (name + ".tmp")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    path.replace(STATE / (name + ".json"))


def main():
    mode, device, image, job, reservation = sys.argv[1:]
    if mode not in {"hold", "normal", "orphan", "recover", "recover-normal"}:
        raise ValueError("Unknown fixed controller scenario")
    signal.alarm(100)
    with ContainerController(device) as control:
        service = PersistentRuntimeService(control.journal, job, reservation, image=image)
        report = {
            "process": control.process_identity,
            "installation": control.journal.identity,
            "owner": service.owner,
            "name": service.name,
            "gpu_executed": False,
        }
        if mode == "hold":
            save("held", report)
            while True:
                control.check()
                time.sleep(0.1)
        if mode.startswith("recover"):
            report["recovery"] = control.journal.recover()
            assert report["recovery"]["ready"] is False and control.journal.load() is None
            if mode == "recover":
                save("finished", report)
                return
        status = service.start(
            [
                PYTHON,
                "-I",
                "/opt/praximesh/container_transport_fixture.py",
                "deadline" if mode == "orphan" else "normal",
            ],
            deadline=time.monotonic() + 80,
            on_tick=control.check,
        )
        with ContainerConnection(service, status, on_tick=control.check) as connection:
            canary = os.urandom(32).hex()
            connection.channel.send({"event": "probe", "canary": canary, "padding": "x" * 1024**2})
            value = connection.channel.receive()
            assert value["event"] == "guest-ready"
            assert value["identity"]["sha256"] == hashlib.sha256(canary.encode()).hexdigest()
            report.update(
                {"container": service.container_id, "guest": value["identity"], "status": status}
            )
            if mode == "orphan":
                connection.channel.send({"event": "hang"})
                assert connection.channel.receive() == {"event": "busy"}
            save("active", report)
            while not (STATE / "continue").exists():
                control.check()
                if time.monotonic() >= status["deadline"] - 8:
                    raise TimeoutError("CPU observer did not release controller")
                time.sleep(0.1)
            if mode == "orphan":
                os._exit(17)
            connection.channel.send({"event": "finish"})
            assert connection.channel.receive() == {"event": "completed", "recovered": True}
            connection.finish()
        report["completion"] = control.journal.recover()
        assert control.journal.load() is None
        save("finished", report)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fixed failure class only; no raw CLI output, paths, credentials or learner data.
        print(type(error).__name__, flush=True)
        raise SystemExit(1) from None
