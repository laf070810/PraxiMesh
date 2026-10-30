"""Credential-free trusted controller; source runs only in the CUDA guest."""

import time

from judge.gpu import evaluate_kernel
from judge.gpu_contract import output_matches, reference_values
from judge.gpu_environment import PrivateRuntime
from judge.gpu_execution_contract import (
    CLEANUP_MARGIN,
    PROTOCOL,
    RECOVERY_MARGIN,
    task_request,
    validate_target,
)
from judge.gpu_policy import execution_digest
from judge.gpu_probes import VECTOR_REFERENCE
from judge.gpu_session import GPUExecutionError
from judge.sandbox import SandboxUnavailable


def recover_device(runtime):
    """A fresh guest/context must compile and execute a fixed known-correct kernel."""
    inputs = {"a": [float(i % 13 - 6) for i in range(257)], "b": [0.25] * 257}
    case = {"inputs": inputs}
    expected = reference_values("vector-add-f32-v1", inputs)
    with runtime.kernel_session("device-recovery", mode="run") as session:
        session.compile(VECTOR_REFERENCE, "vector-add-f32-v1")
        values, _elapsed = session.execute(case)
        if not output_matches(
            values, expected, {"kind": "float", "absolute": 1e-5, "relative": 1e-5}
        ):
            raise SandboxUnavailable("GPU correctness recovery failed")
        session.finish()


def serve(channel, base, archives, deadline):
    """Strict message ordering; no task/result survives failed physical cleanup."""
    runtime = None
    closed = False
    try:
        channel.send(
            {"event": "connected", "protocol": PROTOCOL, "execution_digest": execution_digest()}
        )
        prepare = channel.receive()
        if (
            type(prepare) is not dict
            or set(prepare) != {"event", "target"}
            or prepare["event"] != "prepare"
        ):
            raise SandboxUnavailable("Unexpected controller preparation request")
        validate_target(prepare["target"])
        runtime = PrivateRuntime(base, deadline=deadline, on_tick=channel.connected)
        runtime.prepare(archives[0], archives[1])
        identity = runtime.prepare_gpu(prepare["target"], archives[2])
        channel.send({"event": "prepared", "identity": identity, "target": prepare["target"]})
        request = channel.receive()
        if type(request) is not dict or set(request) != {"event", "problem", "source", "mode"}:
            raise SandboxUnavailable("Unexpected GPU execution request")
        if request != task_request(request["problem"], request["source"], request["mode"]):
            raise SandboxUnavailable("Invalid GPU execution request")
        execution_deadline = deadline - RECOVERY_MARGIN - CLEANUP_MARGIN
        if time.monotonic() >= execution_deadline:
            raise SandboxUnavailable("Insufficient reservation for GPU execution and recovery")

        def execution_tick():
            runtime.tick()
            if time.monotonic() >= execution_deadline:
                raise GPUExecutionError("time_limit")

        with runtime.kernel_session("task", mode=request["mode"]) as session:
            session.on_tick = execution_tick
            result = evaluate_kernel(
                request["problem"], request["source"], request["mode"], session
            )
        # Release the input references before the independent recovery; nothing
        # is persisted and neither expected values nor fixtures reach the guest.
        del request
        runtime.deadline = deadline - CLEANUP_MARGIN
        recover_device(runtime)
        runtime.deadline = deadline
        runtime.close()
        closed = True
        channel.send({"event": "completed", "result": result, "recovered": True})
        if channel.receive() != {"event": "finish"}:
            raise SandboxUnavailable("Unexpected GPU completion acknowledgement")
    finally:
        if runtime is not None and not closed:
            runtime.deadline = deadline
            runtime.close()
