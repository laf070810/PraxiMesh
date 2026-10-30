"""Host-side execution supervision; usable only by an explicitly owned GPU pool.

This adapter does not issue readiness receipts or register a web evaluator.
"""

import copy
import socket
import sys
import uuid
from pathlib import Path

from judge.gpu_device_lock import DeviceLock
from judge.gpu_execution_contract import (
    CONTROLLER_MARGIN,
    PROTOCOL,
    canonical_digest,
    environment_identity,
    expected_identity,
    task_request,
    validate_result,
    validate_target,
)
from judge.gpu_isolation import validate_report
from judge.gpu_monitor import DeviceMonitor, identify
from judge.gpu_namespace import JobWorkspace
from judge.gpu_recovery import recover_workspace
from judge.gpu_service import OwnedJobService
from judge.gpu_transport import PrivateListener
from judge.sandbox import SandboxUnavailable


class GPUExecutionAdapter:
    def __init__(
        self, *, target, directory, runtime_archive, image_archive, nvrtc_archive, podman_version
    ):
        validate_target(target)
        self.target = copy.deepcopy(target)
        self.directory = Path(directory)
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("An absolute private controller workspace is required")
        self.archives = tuple(
            Path(path).resolve(strict=True)
            for path in (runtime_archive, image_archive, nvrtc_archive)
        )
        self.identity = expected_identity(self.archives[1], podman_version)
        self.environment = environment_identity(self.target, self.identity)
        self.fingerprint = canonical_digest(self.environment)

    def evaluate(
        self,
        service,
        *,
        deadline,
        problem,
        source,
        mode,
        on_tick,
        on_begin,
    ):
        if type(service) is not OwnedJobService or service.purpose != "task":
            raise ValueError("An owned task service instance is required")
        request = task_request(problem, source, mode)
        message = self._exchange(
            service, request, deadline=deadline, on_tick=on_tick, on_begin=on_begin
        )
        return copy.deepcopy(message["result"]), copy.deepcopy(self.environment)

    def recover(self, service, *, deadline, on_tick):
        """Run only the fixed correct kernel in a distinct maintenance service."""
        if type(service) is not OwnedJobService or service.purpose != "maintenance":
            raise ValueError("An owned maintenance service instance is required")
        self._exchange(
            service, {"event": "recover"}, deadline=deadline, on_tick=on_tick, on_begin=lambda: None
        )
        return copy.deepcopy(self.environment)

    def check_resources(self, service, *, deadline, on_tick):
        """Fixed infrastructure checks; never a complete GPU readiness receipt."""
        if type(service) is not OwnedJobService or service.purpose != "maintenance":
            raise ValueError("An owned maintenance service instance is required")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            request = {
                "event": "check-resources",
                "canary": uuid.uuid4().hex,
                "port": listener.getsockname()[1],
            }
            message = self._exchange(
                service, request, deadline=deadline, on_tick=on_tick, on_begin=lambda: None
            )
        return copy.deepcopy(message["report"]), copy.deepcopy(self.environment)

    def _exchange(self, service, request, *, deadline, on_tick, on_begin):
        guard = service.guard
        if type(guard) is not DeviceLock or guard.device_uuid != self.target["uuid"]:
            raise ValueError("The selected device's held host lock is required")
        guard.assert_held()
        if (
            environment_identity(self.target, self.identity) != self.environment
            or canonical_digest(self.environment) != self.fingerprint
        ):
            raise SandboxUnavailable("GPU controller configuration changed after preparation")
        if identify(guard.device_uuid) != self.target:
            raise SandboxUnavailable("GPU identity changed after configuration")
        monitor = DeviceMonitor(self.target)
        monitor.observe()

        def tick():
            guard.assert_held()
            on_tick()
            monitor.observe(service)

        with JobWorkspace(self.directory, service) as workspace, PrivateListener() as listener:
            channel = None
            try:
                entry = Path(__file__).with_name("gpu_controller_entry.py")
                command = workspace.command(
                    [
                        sys.executable,
                        "-I",
                        str(entry),
                        str(workspace.path),
                        str(listener.path),
                        *(str(path) for path in self.archives),
                        str(deadline - CONTROLLER_MARGIN),
                    ]
                )
                service.start(command, deadline=deadline, on_tick=tick)
                channel = listener.accept(service, deadline=deadline - 10, on_tick=tick)
                connected = {
                    "event": "connected",
                    "protocol": PROTOCOL,
                    "execution_digest": self.environment["execution_digest"],
                }
                if canonical_digest(channel.receive()) != canonical_digest(connected):
                    raise SandboxUnavailable("Unexpected GPU controller connection identity")
                channel.send({"event": "prepare", "target": self.target})
                prepared = {
                    "event": "prepared",
                    "identity": self.identity,
                    "target": self.target,
                }
                if canonical_digest(channel.receive()) != canonical_digest(prepared):
                    raise SandboxUnavailable("GPU runtime identity does not match its reservation")
                tick()
                on_begin()
                tick()
                channel.send(request)
                message = channel.receive()
                if request["event"] == "check-resources":
                    if (
                        type(message) is not dict
                        or set(message) != {"event", "report", "recovered"}
                        or message["event"] != "resources-checked"
                        or message["recovered"] is not True
                    ):
                        raise SandboxUnavailable("GPU resource checks did not finish recovery")
                    validate_report(message["report"], request, self.target)
                elif request["event"] == "recover":
                    if canonical_digest(message) != canonical_digest(
                        {"event": "recovered", "recovered": True}
                    ):
                        raise SandboxUnavailable("GPU controller did not complete startup recovery")
                else:
                    if (
                        type(message) is not dict
                        or set(message) != {"event", "result", "recovered"}
                        or message["event"] != "completed"
                        or message["recovered"] is not True
                    ):
                        raise SandboxUnavailable("GPU controller did not complete device recovery")
                    validate_result(message["result"], request["problem"], request["mode"])
                channel.send({"event": "finish"})
                channel.expect_eof()
            finally:
                if channel is not None:
                    channel.close()
                service.stop()
                try:
                    workspace.remove_after_stop()
                except PermissionError:
                    # Abandoned mapped files are recovered within the remaining
                    # task budget. If it is exhausted, retain the slot and files.
                    recover_workspace(workspace, deadline=deadline, on_tick=guard.assert_held)
        # Neither a trusted completion frame nor an empty service cgroup proves
        # that the host device has no other process. Check once more before DB IO.
        guard.assert_held()
        monitor.observe()
        return message
