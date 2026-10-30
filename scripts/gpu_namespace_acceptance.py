"""Exercise real controller namespaces and private IPC without using a GPU."""

import hashlib
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_namespace import WORKLOAD_LIMITS, JobWorkspace  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.gpu_transport import PrivateListener, authorize_peer, process_cgroup  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.gpu_control_acceptance import wait_for  # noqa: E402

FIXTURE = ROOT / "scripts/gpu_namespace_fixture.py"
SCENARIOS = ("roundtrip", "disconnect", "extra-message", "deadline", "foreign-peer")
FORBIDDEN_ENV = {
    "DATABASE_URL",
    "PYTHONPATH",
    "HTTP_PROXY",
    "CONTAINER_HOST",
    "DOCKER_HOST",
    "PRAXIMESH_NAMESPACE_CANARY",
}


def stopped(service):
    state = service.inspect()
    return not service.populated() and (
        state is None
        or (state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0")
    )


def verify_no_payload_files(workspace, canary):
    for path in workspace.path.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            raise RuntimeError("Unexpectedly large namespace fixture artifact")
        if canary.encode() in path.read_bytes():
            raise RuntimeError("Transport payload was persisted to the workspace")


def scenario_check(scenario, workspace, service, record):
    started = time.monotonic()
    deadline = started + (16 if scenario == "deadline" else 35)
    with PrivateListener() as listener:
        channel = None
        foreign = None
        try:
            if scenario == "foreign-peer":
                # Same UID, different cgroup; real kernel peer credentials.
                foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                foreign.connect(str(listener.path))
            command = workspace.command(
                [
                    sys.executable,
                    "-I",
                    str(FIXTURE),
                    str(listener.path),
                    "roundtrip" if scenario == "foreign-peer" else scenario,
                ]
            )
            # Deliberately inject only fake environment values before the
            # namespace's env -i boundary, never into the shared user manager.
            command = [
                "/usr/bin/env",
                *(
                    name + "=praximesh-fixed-test-not-a-credential"
                    for name in sorted(FORBIDDEN_ENV)
                ),
                *command,
            ]
            service.start(command, deadline=deadline, on_tick=lambda: None)
            record["service_limits"] = service.limits()

            def connecting():
                if stopped(service):
                    raise SandboxUnavailable("Namespace controller exited before connecting")

            if foreign is not None:
                try:
                    listener.accept(service, deadline=deadline, on_tick=connecting)
                except SandboxUnavailable:
                    record["foreign_peer_refused"] = True
                else:
                    raise RuntimeError("Accepted a peer outside the owned cgroup")
                foreign.close()
                foreign = None
            channel = listener.accept(service, deadline=deadline, on_tick=connecting)
            channel.on_tick = lambda: None
            peer = authorize_peer(channel.sock, service)
            ready = channel.receive()
            if (
                ready["event"] != "ready"
                or ready["uid"] != 0
                or ready["cgroup"] != "0::/manager"
                or ready["workload_limits"] != WORKLOAD_LIMITS
                or ready["interfaces"] != ["lo"]
                or FORBIDDEN_ENV.intersection(ready["environment_names"])
                or any(
                    value == os.readlink("/proc/self/ns/" + name)
                    for name, value in ready["namespaces"].items()
                )
            ):
                raise RuntimeError("Namespace or environment boundary did not hold")
            record.update(
                host_peer_pid=peer,
                host_peer_cgroup=process_cgroup(peer),
                namespace_state=ready,
            )
            # This is opaque synthetic data, never executable source. The large
            # request also exercises partial reads/writes across namespaces.
            canary = "private-memory-only-" + uuid.uuid4().hex
            request = {"canary": canary, "values": [1.234567890123456e-123] * 32768}
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            channel.send(request)
            received = channel.receive()
            if received != {"event": "received", "sha256": hashlib.sha256(encoded).hexdigest()}:
                raise RuntimeError("Cross-namespace payload was corrupted")
            if canary.encode() in Path(f"/proc/{peer}/cmdline").read_bytes():
                raise RuntimeError("Transport payload was placed in a command line")
            verify_no_payload_files(workspace, canary)
            record.update(payload_bytes=len(encoded), payload_persisted=False)
            if scenario in {"roundtrip", "foreign-peer"}:
                channel.send({"event": "finish"})
                channel.expect_eof()
            else:
                if channel.receive() != {"event": "waiting"}:
                    raise RuntimeError("The fixed child process did not start")
                if scenario == "disconnect":
                    channel.close()
                elif scenario == "extra-message":
                    channel.send({"unexpected": "stop the fixture"})
                    # A close with unread rejected data can reset the socket.
                    # Prove process termination below, not a successful result.
                else:
                    record["independent_deadline_seconds"] = service.runtime_limit
                    channel.expect_eof()
            # No explicit stop until the finally block: prove that the process
            # tree exits by itself on disconnect, service deadline or completion.
            wait_for(lambda: stopped(service))
            record.update(
                stopped_without_explicit_stop=True,
                elapsed_seconds=time.monotonic() - started,
                passed=True,
            )
        finally:
            if channel is not None:
                channel.close()
            if foreign is not None:
                foreign.close()


def main():
    if len(sys.argv) != 1:
        raise ValueError("This fixed CPU acceptance takes no GPU or executable arguments")
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-gpu-namespace-" + token)
    out.mkdir(mode=0o700, parents=True)
    directory = Path("/tmp") / ("px-gn-" + token)
    directory.mkdir(mode=0o700)
    report = {
        "scope": "CPU-only real RootlessKit/IPC checks; no CUDA, Podman guest or receipt proof",
        "directory": str(directory),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "judge/gpu_namespace.py",
                "judge/gpu_service.py",
                "judge/gpu_transport.py",
                "scripts/gpu_namespace_fixture.py",
                "scripts/gpu_namespace_acceptance.py",
            )
        },
        "checks": [],
        "passed": False,
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    try:
        for scenario in SCENARIOS:
            record = {"name": scenario, "passed": False}
            report["checks"].append(record)
            with DeviceLock(out / "locks", "GPU-" + str(uuid.uuid4())) as guard:
                service = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
                record["unit"] = service.unit
                with JobWorkspace(directory, service) as workspace:
                    try:
                        scenario_check(scenario, workspace, service, record)
                    finally:
                        record["cleanup"] = service.stop()
                        workspace.remove_after_stop()
                        record["workspace_removed"] = not workspace.path.exists()
                        save()
            print(json.dumps({"passed": scenario}), flush=True)
        directory.rmdir()
        report.update(passed=True, temporary_directory_removed=True)
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        report["temporary_directory_removed"] = not directory.exists()
        save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
