"""Fixed CPU lifecycle fixture inside the trusted control image, never a GPU worker."""

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_compilation import CompilationCancelled, CompilationTask  # noqa: E402
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON  # noqa: E402
from judge.container_controller import STATE, ContainerController  # noqa: E402
from judge.container_journal import PersistentRuntimeService, private  # noqa: E402
from judge.gpu_session import decode_frame  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402


def save(name, value):
    path = STATE / (name + ".tmp")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    path.replace(STATE / (name + ".json"))


def compile_task(control, mode, image, job, reservation):
    descriptor = os.open(STATE / "input.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        private(os.fstat(source.fileno()))
        raw = source.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("Fixed compilation fixture input exceeded its bound")
    message = decode_frame(raw)
    task = CompilationTask(control.journal, job, reservation, image=image)
    report = {
        "process": control.process_identity,
        "installation": control.journal.identity,
        "owner": task.service.owner,
        "name": task.service.name,
        "gpu_executed": False,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "begin_calls": 0,
    }

    def snapshot():
        record = control.journal.load()
        report.update(
            container=task.service.container_id,
            status=task.status,
            original_record={
                key: record[key]
                for key in ("version", "resource_profile", "owner", "image", "container_id")
            },
        )

    def release(marker):
        while not (STATE / marker).exists():
            control.check()
            if time.monotonic() >= task.status["deadline"] - 8:
                raise TimeoutError("CPU compilation observer did not release controller")
            time.sleep(0.1)

    def begin():
        control.check()
        report["begin_calls"] += 1
        snapshot()
        save("prepared", report)
        release("send-source")
        if mode == "compile-cancel-before":
            raise CompilationCancelled("Fixed acceptance cancellation before source")

    def compiled(value):
        snapshot()
        report["compilation"] = value
        save("active", report)
        release("continue")
        if mode == "compile-orphan":
            os._exit(17)
        if mode == "compile-cancel":
            raise CompilationCancelled("Fixed acceptance cancellation after compilation")

    try:
        report["result"] = task.run(
            message,
            deadline=time.monotonic() + 80,
            on_begin=begin,
            on_tick=control.check,
            on_compiled=compiled,
        )
    except CompilationCancelled:
        if mode not in {"compile-cancel-before", "compile-cancel"}:
            raise
        report["cancelled"] = True
    except ValueError:
        if mode != "compile-invalid" or task.service.container_id is not None:
            raise
        report["invalid_request"] = True
    except SandboxUnavailable:
        if mode != "compile-bad-image" or task.service.container_id is not None:
            raise
        report["image_refused"] = True
    report.update(
        container=task.service.container_id,
        cleanup=task.cleanup,
        journal_empty=control.journal.load() is None,
    )
    assert report["journal_empty"] is True
    save("finished", report)


def main():
    mode, device, image, job, reservation = sys.argv[1:]
    compiler_modes = {
        "compile-normal",
        "compile-invalid",
        "compile-bad-image",
        "compile-cancel-before",
        "compile-cancel",
        "compile-orphan",
    }
    if mode not in {"hold", "normal", "orphan", "recover", "recover-normal"} | compiler_modes:
        raise ValueError("Unknown fixed controller scenario")
    signal.alarm(100)
    with ContainerController(device) as control:
        if mode in compiler_modes:
            return compile_task(control, mode, image, job, reservation)
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
