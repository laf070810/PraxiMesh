"""Shared controller lifecycle checks with actual CPU runsc guests; no GPU use."""

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_lifecycle import CHECKS, fault_request, observe_fault, require  # noqa: E402
from judge.gpu_namespace import JobWorkspace  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.gpu_recovery import recover_workspace  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.gpu_transport import PrivateListener  # noqa: E402


def exchange(directory, service, archives, mode, record, *, name, deadline):
    with JobWorkspace(directory, service) as workspace, PrivateListener() as listener:
        channel = None
        record["unit"] = service.unit
        record["workspace"] = str(workspace.path)
        try:
            started = time.monotonic()
            service.start(
                workspace.command(
                    [
                        sys.executable,
                        "-I",
                        str(ROOT / "scripts/gpu_lifecycle_fixture.py"),
                        str(workspace.path),
                        str(listener.path),
                        *map(str, archives),
                        str(deadline - 12),
                        mode,
                    ]
                ),
                deadline=deadline,
                on_tick=service.guard.assert_held,
            )
            record["invocation"] = service.invocation
            channel = listener.accept(
                service, deadline=deadline - 10, on_tick=service.guard.assert_held
            )
            require(channel.receive() == {"event": "connected"}, "CPU fixture did not connect")
            message = channel.receive()
            require(message.get("gpu_prepared") is False, "Unexpected GPU preparation")
            record["runtime"] = message
            if mode == "fault":
                require(message["event"] == "live", "CPU fixture is not live")
                request = fault_request(name, uuid.uuid4().hex)
                channel.send(request)
                record["observation"] = observe_fault(
                    channel,
                    service,
                    request,
                    started=started,
                    deadline=deadline,
                    on_tick=service.guard.assert_held,
                )
            else:
                require(message["event"] == "cpu-recovered", "CPU recovery did not finish")
                require(
                    len(message["cleanup"]) == 1
                    and message["cleanup"][0]["id"] == message["container_id"]
                    and message["cleanup"][0]["stopped"] is True,
                    "CPU recovery container cleanup is incomplete",
                )
                channel.send({"event": "finish"})
                channel.expect_eof()
        finally:
            if channel is not None:
                channel.close()
            record["service_cleanup"] = service.stop()
            try:
                workspace.remove_after_stop()
            except PermissionError:
                record["storage_recovery"] = recover_workspace(
                    workspace, deadline=time.monotonic() + 60, on_tick=service.guard.assert_held
                )
            record["workspace_removed"] = not workspace.path.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--image-archive", required=True, type=Path)
    options = parser.parse_args()
    archives = tuple(
        path.resolve(strict=True) for path in (options.runtime_archive, options.image_archive)
    )
    token = uuid.uuid4().hex[:12]
    output = ROOT / "test-results" / ("px-gpu-lifecycle-" + token)
    output.mkdir(mode=0o700, parents=True)
    directory = Path("/tmp") / ("px-lc-" + token)
    directory.mkdir(mode=0o700)
    report = {
        "scope": "Shared controller faults with CPU runsc guests, storage cleanup and new CPU recovery; no CUDA or receipt",
        "directory": str(directory),
        "execution_digest": execution_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "judge/gpu_lifecycle.py",
                "judge/gpu_adapter.py",
                "judge/gpu_controller.py",
                "judge/gpu_service.py",
                "judge/gpu_transport.py",
                "judge/gpu_environment.py",
                "judge/gpu_recovery.py",
                "scripts/gpu_lifecycle_acceptance.py",
                "scripts/gpu_lifecycle_fixture.py",
                "scripts/gpu_runtime_cpu_guest.py",
            )
        },
        "checks": [],
        "passed": False,
    }

    def save():
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps({"report": str(output / "result.json")}), flush=True)
    save()
    try:
        with DeviceLock(output / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            for name in CHECKS:
                row = {"name": name, "fault": {}, "recovery": {}, "passed": False}
                report["checks"].append(row)
                save()
                job, reservation = uuid.uuid4(), uuid.uuid4()
                fault = OwnedJobService(guard, job, reservation, purpose="maintenance")
                exchange(
                    directory,
                    fault,
                    archives,
                    "fault",
                    row["fault"],
                    name=name,
                    deadline=time.monotonic() + 35,
                )
                # No new invocation until the old cgroup and workspace are gone.
                recovery = OwnedJobService(guard, job, reservation, purpose="maintenance")
                exchange(
                    directory,
                    recovery,
                    archives,
                    "cpu-recovery",
                    row["recovery"],
                    name=name,
                    deadline=time.monotonic() + 45,
                )
                require(fault.invocation != recovery.invocation, "Old invocation was reused")
                require(
                    row["fault"]["runtime"]["container_id"]
                    != row["recovery"]["runtime"]["container_id"],
                    "Old container was reused for recovery",
                )
                require(
                    row["fault"]["runtime"]["identity"] == row["recovery"]["runtime"]["identity"],
                    "CPU runtime identity changed during recovery",
                )
                row["passed"] = True
                save()
                print(json.dumps({"check": name, "passed": True}), flush=True)
        report["passed"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if not any(directory.iterdir()):
            directory.rmdir()
        report["temporary_directory_removed"] = not directory.exists()
        save()
    print(json.dumps({"passed": True, "report": str(output / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
