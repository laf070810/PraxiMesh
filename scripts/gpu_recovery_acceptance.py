"""Real crash/storage recovery using fixed CPU runsc guests, without a GPU."""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_namespace import JobWorkspace  # noqa: E402
from judge.gpu_recovery import recover_workspace  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.gpu_transport import PrivateListener  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.gpu_control_acceptance import wait_for  # noqa: E402
from scripts.gpu_namespace_acceptance import stopped  # noqa: E402


def abandoned_recovery(workspace, record):
    original = workspace.service
    service = OwnedJobService(
        original.guard, original.job_id, original.reservation_token, purpose="recovery"
    )
    # Start the real fixed cleanup entry, authenticate its peer, then terminate
    # it before authorizing deletion. Leave its workspace for the next owner.
    with JobWorkspace(workspace.directory, service) as abandoned, PrivateListener() as listener:
        record["interrupted_recovery_unit"] = service.unit
        channel = None
        try:
            deadline = time.monotonic() + 45
            service.start(
                abandoned.command(
                    [
                        sys.executable,
                        "-I",
                        str(ROOT / "judge/gpu_recovery_entry.py"),
                        str(listener.path),
                        str(deadline - 12),
                    ]
                ),
                deadline=deadline,
                on_tick=original.guard.assert_held,
            )
            channel = listener.accept(
                service, deadline=deadline - 10, on_tick=original.guard.assert_held
            )
            assert channel.receive() == {"event": "ready"}
            record["interrupted_recovery_cleanup"] = service.stop()
            assert abandoned.path.exists()
        finally:
            if channel is not None:
                channel.close()
            service.stop()


def scenario(name, directory, guard, archives, record, save):
    service = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
    record.update(name=name, passed=False, unit=service.unit)
    with JobWorkspace(directory, service) as workspace, PrivateListener() as listener:
        record["workspace"] = str(workspace.path)
        channel = None
        try:
            started = time.monotonic()
            deadline = started + (28 if name == "deadline" else 90)
            service.start(
                workspace.command(
                    [
                        sys.executable,
                        "-I",
                        str(ROOT / "scripts/gpu_crash_fixture.py"),
                        str(workspace.path),
                        str(listener.path),
                        *map(str, archives),
                        str(deadline - 12),
                    ]
                ),
                deadline=deadline,
                on_tick=guard.assert_held,
            )
            channel = listener.accept(service, deadline=deadline - 10, on_tick=guard.assert_held)
            assert channel.receive() == {"event": "connected"}
            message = channel.receive()
            assert message["event"] == "live", message
            record.update(
                identity=message["identity"],
                container_id=message["container_id"],
                guest_was_running=True,
            )
            mapped = workspace.path / "data/mapped-proof"
            record["mapped_uid"] = mapped.stat().st_uid
            assert record["mapped_uid"] != os.getuid()
            try:
                list(mapped.iterdir())
            except PermissionError:
                record["host_cannot_read_mapped_directory"] = True
            else:
                raise RuntimeError(
                    "Mapped-file fixture did not exercise the host permission boundary"
                )
            # An active service cannot enter the destructive recovery path.
            try:
                recover_workspace(
                    workspace, deadline=time.monotonic() + 50, on_tick=guard.assert_held
                )
            except SandboxUnavailable as error:
                assert "Stop the owned" in str(error)
                record["live_recovery_rejected"] = True
            else:
                raise RuntimeError("A live task was accepted for storage recovery")
            if name == "controller-exit":
                channel.send({"event": "exit"})
            else:
                channel.send({"event": "wait"})
                if name == "service-stop":
                    record["explicit_stop"] = service.stop()
            wait_for(lambda: stopped(service), seconds=35)
            record["all_task_processes_stopped"] = True
            record["stop_elapsed_seconds"] = time.monotonic() - started
            channel.close()
            channel = None
            if name == "service-stop":
                abandoned_recovery(workspace, record)
            # Reopen through a replacement controller, with only the persisted
            # reservation and lock; no original in-memory container registry.
            replacement = OwnedJobService(guard, service.job_id, service.reservation_token)
            with JobWorkspace(directory, replacement, create=False) as reopened:
                record["recovery"] = recover_workspace(
                    reopened, deadline=time.monotonic() + 60, on_tick=guard.assert_held
                )
            record["workspace_removed"] = not workspace.path.exists()
            assert record["workspace_removed"]
            record["passed"] = True
        finally:
            if channel is not None:
                channel.close()
            record["service_cleanup"] = service.stop()
            if workspace.path.exists():
                # Do not hide the original failure. Preserve an exact owned
                # directory if even the explicit recovery cannot finish.
                try:
                    record["failure_cleanup"] = recover_workspace(
                        workspace, deadline=time.monotonic() + 60, on_tick=guard.assert_held
                    )
                except Exception as error:
                    record["cleanup_error"] = type(error).__name__ + ": " + str(error)
            save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--image-archive", required=True, type=Path)
    options = parser.parse_args()
    archives = [
        options.runtime_archive.resolve(strict=True),
        options.image_archive.resolve(strict=True),
    ]
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-gpu-recovery-" + token)
    out.mkdir(mode=0o700, parents=True)
    directory = Path("/tmp") / ("px-rc-" + token)
    directory.mkdir(mode=0o700)
    report = {
        "scope": "Fixed CPU runsc process/storage crash recovery; no GPU, lease release or receipt",
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
                "judge/gpu_recovery.py",
                "judge/gpu_recovery_entry.py",
                "scripts/gpu_runtime_cpu_guest.py",
                "scripts/gpu_crash_fixture.py",
                "scripts/gpu_recovery_acceptance.py",
            )
        },
        "passed": False,
        "checks": [],
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    try:
        with DeviceLock(out / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            for name in ("controller-exit", "service-stop", "deadline"):
                record = {}
                report["checks"].append(record)
                scenario(name, directory, guard, archives, record, save)
        report["passed"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if not any(directory.iterdir()):
            directory.rmdir()
        report["temporary_directory_removed"] = not directory.exists()
        save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
