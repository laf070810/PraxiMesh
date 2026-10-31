"""Actual trusted container communication and fixed CPU guest recovery; no GPU."""

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON  # noqa: E402
from judge.container_runtime_service import SOURCES, RuntimeContainerService, contract_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_transport import FRAME_BYTES  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.container_runtime_acceptance import build_image, read_report  # noqa: E402
from scripts.container_supervision_acceptance import gone, owned_cleanup, tree, wait_exit  # noqa: E402


def scenario(engine, image, directory, mode, report):
    directory.mkdir(mode=0o700)
    with DeviceLock(directory / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        service = RuntimeContainerService(
            guard, uuid.uuid4(), uuid.uuid4(), engine=engine, image=image
        )
        connection = None
        record = {"mode": mode}
        try:
            status = service.start(
                [PYTHON, "-I", "/opt/praximesh/container_transport_fixture.py", mode],
                deadline=time.monotonic() + (22 if mode == "deadline" else 40),
                on_tick=lambda: None,
            )
            cancelled = False

            def tick():
                if cancelled:
                    raise SandboxUnavailable("Fixed acceptance lease cancelled")

            if mode == "wrong-start":
                try:
                    ContainerConnection(
                        service, {**status, "child": status["child"] + 1}, on_tick=tick
                    )
                except SandboxUnavailable as error:
                    assert "startup identity" in str(error), str(error)
                else:
                    raise AssertionError("Mismatched startup identity was accepted")
            else:
                connection = ContainerConnection(service, status, on_tick=tick)
                if mode == "oversized":
                    connection.channel.sock.sendall(struct.pack("!I", FRAME_BYTES + 1))
                else:
                    canary = os.urandom(32).hex()
                    request = {"event": "probe", "canary": canary, "padding": "x" * 1024**2}
                    connection.channel.send(request)
                    result = connection.channel.receive()
                    assert (
                        result["event"] == "guest-ready" and result["padding"] == request["padding"]
                    )
                    assert (
                        result["identity"]["sha256"] == hashlib.sha256(canary.encode()).hexdigest()
                    )
                    assert result["identity"]["private_file_visible"] is False
                    processes = tree(engine, service)
                    record["processes"] = processes
                    connection.channel.send({"event": "finish" if mode == "normal" else "hang"})
                    if mode == "normal":
                        assert connection.channel.receive() == {
                            "event": "completed",
                            "recovered": True,
                        }
                        connection.finish()
                    else:
                        assert connection.channel.receive() == {"event": "busy"}
                        if mode == "disconnect":
                            connection.close()
                        elif mode == "client-exit":
                            connection.process.kill()
                            connection.process.wait(timeout=3)
                        elif mode == "cancel":
                            cancelled = True
                            try:
                                connection.channel.receive()
                            except SandboxUnavailable as error:
                                assert "lease cancelled" in str(error), str(error)
                            else:
                                raise AssertionError("Cancellation callback was ignored")
                            connection.close()
                        elif mode == "trailing":
                            connection.channel.send({"event": "unexpected"})
                        elif mode == "deadline":
                            wait_exit(service, 124, status["deadline"] + 5)
                            gone(processes)
                            record["independent_deadline"] = True
            if mode != "deadline":
                result = read_report(engine, service, "transport", time.monotonic() + 10)
                record["controller"] = result
                assert "unexpected" not in result, result
                if mode in {"wrong-start", "oversized"}:
                    assert result["guest_started"] is False and not result["cleanup"], result
                    assert "failure" in result, result
                    if mode == "oversized":
                        assert "budget" in result["failure"], result
                else:
                    assert result["guest_started"] is True and result["cleanup"] == [
                        {"id": "control", "stopped": True, "removed": True}
                    ], result
                    if mode == "normal":
                        assert result["completed"] is True, result
                    else:
                        assert "failure" in result, result
                        assert (
                            "Unexpected message" if mode == "trailing" else "disconnected"
                        ) in result["failure"], result
                service.stop()
                if record.get("processes"):
                    gone(record["processes"])
            service.remove()
            report["cleanup"].append({"container": service.container_id, "removed": True})
            record["passed"] = True
            return record
        except BaseException as error:
            record["failure"] = type(error).__name__ + ": " + str(error)
            if service.container_id:
                try:
                    record["controller"] = read_report(
                        engine, service, "transport", time.monotonic() + 2
                    )
                except Exception:
                    pass
            report["failed_scenario"] = record
            raise
        finally:
            if connection is not None:
                connection.close()
            owned_cleanup(engine, service, image, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-transport-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "checks": [],
        "cleanup": [],
        "contract": contract_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                *SOURCES,
                "scripts/container_runtime_acceptance.py",
                "scripts/container_transport_acceptance.py",
            )
        },
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, out, out.name)
        report["image"] = image
        for mode in (
            "normal",
            "wrong-start",
            "oversized",
            "disconnect",
            "client-exit",
            "cancel",
            "trailing",
            "deadline",
            "normal",
        ):
            result = scenario(engine, image, out / str(len(report["checks"])), mode, report)
            report["checks"].append(result)
            save()
            print("PASS " + mode, flush=True)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            if image is not None:
                engine.run("image", "rm", out.name)
                report["owned_image_tag_removed"] = True
        finally:
            save()
    report["passed"] = True
    save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
