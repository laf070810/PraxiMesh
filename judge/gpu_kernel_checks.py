"""Fixed CUDA maintenance checks; never a complete readiness receipt.

Only the trusted controller chooses these sources. Their execution is confined
to PrivateRuntime CUDA sessions; host code builds inputs and checks results.
"""

import re
import time

from judge.gpu import evaluate_kernel
from judge.gpu_contract import reference_values
from judge.gpu_probes import VECTOR_REFERENCE, correctness_fixtures, fault_fixtures
from judge.gpu_session import GPUDriverExited
from judge.gpu_task_protocol import validate_result
from judge.sandbox import SandboxUnavailable

CHECKS = (
    "vector-reference",
    "vector-subtraction",
    "vector-unwritten",
    "matrix-reference",
    "matrix-wrong-stride",
    "compile-error",
    "missing-entrypoint",
    "public-run",
    "maximum-vector",
    "maximum-matrix",
    "printf-spoof",
    "device-heap-bound",
    "illegal-address",
    "module-memory-limit",
    "invalid-ptx",
    "kernel-timeout",
    "stderr-budget",
    "cancel",
    "attach-death",
)
INTERRUPTIONS = {"cancel", "attach-death"}


def check_request(name, challenge):
    if (
        type(name) is not str
        or name not in CHECKS
        or type(challenge) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", challenge)
    ):
        raise ValueError("A fixed CUDA check name and fresh challenge are required")
    return {"event": "check-kernel", "name": name, "challenge": challenge}


def problem_for(fixture):
    inputs = fixture["inputs"] or [{"a": [1], "b": [2]}]
    if len(inputs) == 1:
        inputs = inputs * 2
    return {
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "entrypoint": "solve",
        "operation": fixture["operation"],
        "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        "tests": [
            {
                "name": "public" if index == 0 else f"protected-case-{index}",
                "public": index == 0,
                "inputs": values,
                "expected": reference_values(fixture["operation"], values),
            }
            for index, values in enumerate(inputs)
        ],
    }


def case_for(name):
    """Return new controller-owned data; callers cannot replace a named source."""
    if type(name) is not str or name not in CHECKS or name in INTERRUPTIONS:
        raise ValueError("Unknown graded CUDA check")
    fixtures = {item["name"]: item for item in correctness_fixtures()}
    vector = fixtures["vector-reference"]
    matrix = fixtures["matrix-reference"]
    if name in fixtures:
        fixture = fixtures[name]
        verdict = {2: "runtime_error", 3: "syntax_error"}.get(
            fixture["exit_code"], "accepted" if fixture["correct"] else "wrong_answer"
        )
    elif name == "public-run":
        fixture = dict(vector, inputs=vector["inputs"][1:])
        verdict = "accepted"
    elif name == "maximum-vector":
        fixture = dict(
            vector,
            inputs=[
                {"a": [float(i % 19 - 9) / 4 for i in range(4096)], "b": [0.25] * 4096},
                {"a": [0.5] * 4096, "b": [float(i % 13 - 6) / 2 for i in range(4096)]},
            ],
        )
        verdict = "accepted"
    elif name == "maximum-matrix":
        fixture = dict(
            matrix,
            inputs=[
                matrix["inputs"][0],
                {
                    "m": 64,
                    "n": 64,
                    "k": 64,
                    "a": [float(i % 7 - 3) / 4 for i in range(4096)],
                    "b": [float(i % 11 - 5) / 2 for i in range(4096)],
                },
            ],
        )
        verdict = "accepted"
    else:
        faults = {item["name"]: item for item in fault_fixtures()}
        if name == "stderr-budget":
            fixture = dict(
                faults["printf-spoof"],
                source=VECTOR_REFERENCE.replace(
                    "if (i < n)",
                    'if (i == 0) for(int j=0; j<4096; ++j) printf("device-output-limit-canary\\n");\n'
                    "    if (i < n)",
                ),
                compiled=True,
            )
            verdict = "output_limit"
        else:
            fixture = faults[name]
            verdict = {
                "printf-spoof": "accepted",
                "device-heap-bound": "accepted",
                "illegal-address": "runtime_error",
                "module-memory-limit": "memory_limit",
                "invalid-ptx": "runtime_error",
                "kernel-timeout": "time_limit",
            }[name]
    return {
        "problem": problem_for(fixture),
        "source": fixture["source"],
        "mode": "run" if name == "public-run" else "submit",
        "verdict": verdict,
        "compiled": fixture.get("compiled", False) or verdict in {"accepted", "wrong_answer"},
    }


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def validate_check_report(report, request):
    check_request(request["name"], request["challenge"])
    name = request["name"]
    field = "interruption" if name in INTERRUPTIONS else "result"
    require(
        type(report) is dict
        and set(report) == {"name", "challenge", "cleanup", field}
        and report["name"] == name
        and report["challenge"] == request["challenge"],
        "CUDA check report does not match the request",
    )
    cleanup = report["cleanup"]
    require(
        type(cleanup) is dict
        and set(cleanup) == {"id", "running_before_cleanup", "exit_code", "stopped"}
        and type(cleanup["id"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", cleanup["id"])
        and type(cleanup["running_before_cleanup"]) is bool
        and type(cleanup["exit_code"]) is int
        and cleanup["stopped"] is True,
        "CUDA check container cleanup was not verified",
    )
    if name in INTERRUPTIONS:
        require(
            cleanup["running_before_cleanup"] is True,
            "CUDA interruption did not require physical container cleanup",
        )
    validate_outcome(report, request)


def validate_outcome(report, request):
    """Shared check semantics after the caller verifies its own cleanup schema."""
    name = request["name"]
    cleanup = report["cleanup"]
    if name in INTERRUPTIONS:
        require(
            report["interruption"] == name,
            "CUDA interruption does not match the requested check",
        )
        return
    case = case_for(name)
    result = report["result"]
    validate_result(result, case["problem"], case["mode"])
    require(result["verdict"] == case["verdict"], "CUDA check returned an unexpected verdict")
    if name == "module-memory-limit":
        require(
            cleanup["exit_code"] == 4,
            "CUDA module memory check did not observe the driver memory failure",
        )
    if case["compiled"]:
        require(
            result.get("compile_initialization_ms", 0) > 0,
            "CUDA check did not complete compilation before execution",
        )
    if "performance" in result:
        require(
            all(value > 0 for value in result["performance"]["samples_ms"]),
            "CUDA check has no positive measured samples",
        )


class _Cancelled(Exception):
    pass


def interrupt_session(runtime, session, name, *, before_interrupt=None):
    """Interrupt an already compiled session, never an arbitrary host process."""
    fixture = next(item for item in fault_fixtures() if item["name"] == "kernel-timeout")
    session.compile(fixture["source"], fixture["operation"])
    armed = time.monotonic()
    triggered = False

    def tick():
        nonlocal triggered
        runtime.tick()
        if not triggered and time.monotonic() >= armed + 0.5:
            require(session.process.poll() is None, "CUDA attach client exited before interruption")
            if before_interrupt is not None:
                before_interrupt()
                require(
                    session.process.poll() is None, "CUDA attach client exited during observation"
                )
            if name == "cancel":
                triggered = True
                raise _Cancelled
            session.process.kill()
            session.process.wait(timeout=2)
            triggered = True

    session.on_tick = tick
    try:
        session.execute({"inputs": fixture["inputs"][0]})
    except _Cancelled:
        if name != "cancel" or not triggered:
            raise
    except GPUDriverExited:
        if name != "attach-death" or not triggered:
            raise
    else:
        raise SandboxUnavailable("Interrupted CUDA session unexpectedly returned a result")
    runtime.tick()
    return name


def check_kernel(runtime, request):
    """Run one named check. The caller must still recover CUDA and the service."""
    require(
        type(request) is dict
        and set(request) == {"event", "name", "challenge"}
        and request == check_request(request["name"], request["challenge"]),
        "Unexpected CUDA check request",
    )
    runtime.tick()
    name = request["name"]
    case = None if name in INTERRUPTIONS else case_for(name)
    mode = "run" if case is None else case["mode"]
    report = {"name": name, "challenge": request["challenge"]}
    with runtime.kernel_session("startup-check", mode=mode) as session:
        cleanup = session.cleanup
        identity = runtime.containers["startup-check"]["id"]

        def capture_cleanup():
            stopped = cleanup()
            require(stopped["id"] == identity, "CUDA cleanup changed the owned container")
            report["cleanup"] = stopped
            return stopped

        session.cleanup = capture_cleanup
        if case is None:
            report["interruption"] = interrupt_session(runtime, session, name)
        else:
            report["result"] = evaluate_kernel(case["problem"], case["source"], mode, session)
    runtime.tick()
    validate_check_report(report, request)
    return report
