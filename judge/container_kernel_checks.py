"""Fixed CUDA maintenance cases with original nested-runsc cleanup evidence."""

from copy import deepcopy

from judge.gpu import evaluate_kernel
from judge.gpu_kernel_checks import (
    INTERRUPTIONS,
    case_for,
    check_request,
    interrupt_session,
    require,
    validate_outcome,
)

SCOPE = "container-kernel-check-v1"
LABEL = "startup-check"


def validate_check_report(report, request):
    check_request(request["name"], request["challenge"])
    field = "interruption" if request["name"] in INTERRUPTIONS else "result"
    fields = {"scope", "name", "challenge", "cleanup", field}
    if field == "interruption":
        fields.add("interruption_observation")
    require(
        type(report) is dict
        and set(report) == fields
        and report["scope"] == SCOPE
        and report["name"] == request["name"]
        and report["challenge"] == request["challenge"],
        "Container CUDA check report does not match its request",
    )
    cleanup = report["cleanup"]
    require(
        type(cleanup) is dict
        and set(cleanup)
        == {"scope", "id", "running_before_cleanup", "exit_code", "stopped", "removed"}
        and cleanup["scope"] == "runsc-guest-v1"
        and cleanup["id"] == LABEL
        and type(cleanup["running_before_cleanup"]) is bool
        and type(cleanup["exit_code"]) is int
        and cleanup["stopped"] is True
        and cleanup["removed"] is True,
        "Container CUDA check did not verify its original guest cleanup",
    )
    if field == "interruption":
        observation = report["interruption_observation"]
        require(
            type(observation) is dict
            and set(observation) == {"scope", "id", "status", "attach_alive"}
            and observation["scope"] == "runsc-guest-v1"
            and observation["id"] == LABEL
            and observation["status"] == "running"
            and observation["attach_alive"] is True,
            "Container CUDA interruption did not observe its live original guest",
        )
    validate_outcome(report, request)


def check_kernel(runtime, request):
    require(
        type(request) is dict
        and set(request) == {"event", "name", "challenge"}
        and request == check_request(request["name"], request["challenge"]),
        "Unexpected container CUDA check request",
    )
    runtime.tick()
    name = request["name"]
    case = None if name in INTERRUPTIONS else case_for(name)
    mode = "run" if case is None else case["mode"]
    report = {"scope": SCOPE, "name": name, "challenge": request["challenge"]}
    with runtime.kernel_session(LABEL, mode=mode) as session:
        cleanup = session.cleanup
        original = runtime.containers[LABEL]

        def capture_cleanup():
            require(runtime.containers[LABEL] is original, "CUDA check guest identity changed")
            stopped = cleanup()
            require(
                stopped == {"id": LABEL, "stopped": True, "removed": True}
                and original.get("closed") is True,
                "CUDA check guest cleanup was not confirmed",
            )
            report["cleanup"] = deepcopy(original.get("cleanup_evidence"))
            return stopped

        session.cleanup = capture_cleanup
        if case is None:

            def observe():
                require(runtime.containers[LABEL] is original, "CUDA check guest identity changed")
                state = runtime.inspect(LABEL)
                require(state["status"] == "running", "CUDA guest stopped before interruption")
                require(session.process.poll() is None, "CUDA attach stopped before interruption")
                report["interruption_observation"] = {
                    "scope": "runsc-guest-v1",
                    "id": LABEL,
                    "status": state["status"],
                    "attach_alive": True,
                }

            report["interruption"] = interrupt_session(
                runtime, session, name, before_interrupt=observe
            )
        else:
            report["result"] = evaluate_kernel(case["problem"], case["source"], mode, session)
    runtime.tick()
    validate_check_report(report, request)
    return report
