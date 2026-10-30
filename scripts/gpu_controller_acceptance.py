"""Reject an out-of-order request in the actual controller namespace; no GPU work."""

import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_execution_contract import PROTOCOL  # noqa: E402
from judge.gpu_namespace import JobWorkspace  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.gpu_transport import PrivateListener  # noqa: E402
from scripts.gpu_control_acceptance import wait_for  # noqa: E402
from scripts.gpu_namespace_acceptance import stopped  # noqa: E402


def main():
    if len(sys.argv) != 1:
        raise ValueError("This rejection-only acceptance has no device or executable argument")
    token = uuid.uuid4().hex[:12]
    output = ROOT / "test-results" / ("px-gpu-controller-" + token)
    output.mkdir(mode=0o700, parents=True)
    directory = Path("/tmp") / ("px-gc-" + token)
    directory.mkdir(mode=0o700)
    report = {
        "scope": "Actual controller import, peer authentication and early rejection; no GPU preparation or task execution",
        "directory": str(directory),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "judge/gpu_controller.py",
                "judge/gpu_controller_entry.py",
                "judge/gpu_execution_contract.py",
                "judge/gpu_adapter.py",
                "core/gpu_execution.py",
                "judge/gpu_policy.py",
                "scripts/gpu_controller_acceptance.py",
            )
        },
        "execution_digest": execution_digest(),
        "passed": False,
    }

    def save():
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps({"report": str(output / "result.json")}), flush=True)
    save()
    try:
        with DeviceLock(output / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            service = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
            report["unit"] = service.unit
            with JobWorkspace(directory, service) as workspace, PrivateListener() as listener:
                channel = None
                try:
                    deadline = time.monotonic() + 35
                    service.start(
                        workspace.command(
                            [
                                sys.executable,
                                "-I",
                                str(ROOT / "judge/gpu_controller_entry.py"),
                                str(workspace.path),
                                str(listener.path),
                                "/not-opened/runsc",
                                "/not-opened/image",
                                "/not-opened/nvrtc",
                                str(deadline - 12),
                            ]
                        ),
                        deadline=deadline,
                        on_tick=guard.assert_held,
                    )
                    channel = listener.accept(
                        service, deadline=deadline - 10, on_tick=guard.assert_held
                    )
                    assert channel.receive() == {
                        "event": "connected",
                        "protocol": PROTOCOL,
                        "execution_digest": report["execution_digest"],
                    }
                    report["authenticated_controller_connected"] = True
                    channel.send({"event": "execute"})
                    assert channel.receive() == {"event": "failed"}
                    channel.expect_eof()
                    wait_for(lambda: stopped(service))
                    assert not (workspace.path / "runtime-owner.json").exists()
                    assert not (workspace.path / "data").exists()
                    report.update(
                        passed=True,
                        rejected_before_runtime_preparation=True,
                        stopped_without_explicit_stop=True,
                    )
                finally:
                    if channel is not None:
                        channel.close()
                    report["cleanup"] = service.stop()
                    workspace.remove_after_stop()
                    report["workspace_removed"] = not workspace.path.exists()
                    save()
    except BaseException as error:
        report.update(passed=False, error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        if not any(directory.iterdir()):
            directory.rmdir()
        report["temporary_directory_removed"] = not directory.exists()
        save()
    print(json.dumps({"passed": True, "report": str(output / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
