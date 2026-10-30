"""Check offline runsc runtime plumbing with fixed CPU guests and no GPU access."""

import argparse
import hashlib
import json
import socket
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_namespace import JobWorkspace  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.gpu_transport import PrivateListener  # noqa: E402
from scripts.gpu_control_acceptance import wait_for  # noqa: E402
from scripts.gpu_namespace_acceptance import stopped  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--image-archive", required=True, type=Path)
    options = parser.parse_args()
    runtime_archive = options.runtime_archive.resolve(strict=True)
    image_archive = options.image_archive.resolve(strict=True)
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-gpu-runtime-" + token)
    out.mkdir(mode=0o700, parents=True)
    directory = Path("/tmp") / ("px-gr-" + token)
    directory.mkdir(mode=0o700)
    report = {
        "scope": "Fixed CPU guest in actual offline runsc; no GPU, CUDA recovery or receipt proof",
        "directory": str(directory),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "judge/gpu_artifacts.py",
                "judge/gpu_command.py",
                "judge/gpu_environment.py",
                "judge/gpu_namespace.py",
                "judge/gpu_service.py",
                "judge/gpu_session.py",
                "judge/gpu_transport.py",
                "scripts/gpu_runtime_cpu_guest.py",
                "scripts/gpu_runtime_fixture.py",
                "scripts/gpu_runtime_acceptance.py",
            )
        },
        "passed": False,
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    started = time.monotonic()
    try:
        with DeviceLock(out / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            service = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
            report["unit"] = service.unit
            with JobWorkspace(directory, service) as workspace, PrivateListener() as listener:
                channel = None
                try:
                    deadline = time.monotonic() + 180
                    command = workspace.command(
                        [
                            sys.executable,
                            "-I",
                            str(ROOT / "scripts/gpu_runtime_fixture.py"),
                            str(workspace.path),
                            str(listener.path),
                            str(runtime_archive),
                            str(image_archive),
                            str(deadline - 12),
                        ]
                    )
                    service.start(command, deadline=deadline, on_tick=guard.assert_held)
                    channel = listener.accept(
                        service, deadline=deadline - 10, on_tick=guard.assert_held
                    )
                    if channel.receive() != {"event": "connected"}:
                        raise RuntimeError("Private runtime did not establish its control channel")
                    prepared = channel.receive()
                    if prepared["event"] == "ready":
                        report["prepared_identity"] = prepared["identity"]
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as private_host:
                            private_host.bind(("127.0.0.1", 0))
                            private_host.listen(1)
                            channel.send(
                                {"canary": uuid.uuid4().hex, "port": private_host.getsockname()[1]}
                            )
                            message = channel.receive()
                    else:
                        message = prepared
                    if message["event"] != "finished":
                        raise RuntimeError("Private runtime did not return a complete result")
                    report["controller"] = message["result"]
                    channel.send({"event": "finish"})
                    channel.expect_eof()
                    wait_for(lambda: stopped(service))
                    report["stopped_without_explicit_stop"] = True
                    if not report["controller"]["passed"]:
                        raise RuntimeError(
                            "Fixed CPU runtime acceptance failed; inspect the private report"
                        )
                    report["passed"] = True
                finally:
                    if channel is not None:
                        channel.close()
                    report["service_cleanup"] = service.stop()
                    workspace.remove_after_stop()
                    report["workspace_removed"] = not workspace.path.exists()
                    save()
    except BaseException as error:
        report.update(passed=False, error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        report["temporary_directory_removed"] = not directory.exists()
        report["elapsed_seconds"] = time.monotonic() - started
        save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
