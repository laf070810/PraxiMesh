"""Trusted task computation; protected answers never enter the guest protocol."""

import time

from judge.container_kernel_protocol import PROTOCOL, checked_completion, validate_request
from judge.gpu import evaluate_kernel
from judge.gpu_recovery_kernel import recover_device
from judge.gpu_session import GPUExecutionError
from judge.gpu_task_protocol import CLEANUP_MARGIN, RECOVERY_MARGIN
from judge.sandbox import SandboxUnavailable


def serve_kernel(channel, runtime, identity):
    """Return a result only after a new correct guest and all runtime cleanup."""
    channel.send(identity)
    request = validate_request(channel.receive())
    deadline = runtime.deadline
    maintenance = request["event"] == "recover"
    try:
        if not maintenance:
            execution_deadline = deadline - RECOVERY_MARGIN - CLEANUP_MARGIN
            if time.monotonic() >= execution_deadline:
                raise SandboxUnavailable("Insufficient budget for kernel task and recovery")

            def execution_tick():
                if time.monotonic() >= execution_deadline:
                    raise GPUExecutionError("time_limit")
                runtime.tick()
                channel.connected()
                if time.monotonic() >= execution_deadline:
                    raise GPUExecutionError("time_limit")

            with runtime.kernel_session(
                "task", mode=request["mode"], on_tick=execution_tick
            ) as session:
                result = evaluate_kernel(
                    request["problem"], request["source"], request["mode"], session
                )
        # The task guest has stopped before an independently compiled correct
        # recovery guest is created. A raw runtime error cannot stand in for it.
        runtime.deadline = deadline - CLEANUP_MARGIN
        runtime.on_tick = channel.connected
        recover_device(runtime)
        runtime.deadline = deadline
        runtime.close()
        completed = {
            "event": "kernel-completed",
            "protocol": PROTOCOL,
            "recovered": True,
            "ready": False,
            "cleanup": runtime.kernel_cleanup,
        }
        if not maintenance:
            completed["result"] = result
        checked_completion(completed, request)
        del request
        channel.send(completed)
        if channel.receive() != {"event": "finish"}:
            raise SandboxUnavailable("Unexpected kernel completion acknowledgement")
    finally:
        runtime.deadline = deadline
        runtime.close()
