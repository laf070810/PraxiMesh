"""Fixed controller faults and independent termination observation.

The production caller must first verify an open CUDA session. CPU acceptance
uses the same protocol with a fixed CPU guest and cannot certify GPU readiness.
"""

import math
import os
import re
import time

from judge.gpu_execution_contract import canonical_digest
from judge.gpu_service import STOP_SECONDS
from judge.sandbox import SandboxUnavailable

CHECKS = ("controller-exit", "transport-loss", "service-deadline")
FAULT_SECONDS = 90
RECOVERY_SECONDS = 90
STORAGE_SECONDS = 30


def fault_request(name, challenge):
    if (
        type(name) is not str
        or name not in CHECKS
        or type(challenge) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", challenge)
    ):
        raise ValueError("A fixed controller fault and fresh challenge are required")
    return {"event": "check-controller", "name": name, "challenge": challenge}


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def validate_request(request):
    require(
        type(request) is dict
        and set(request) == {"event", "name", "challenge"}
        and request == fault_request(request["name"], request["challenge"]),
        "Unexpected controller fault request",
    )


def phase(request, event):
    return {**request, "event": event}


def trigger_fault(channel, request):
    """Only called after an owned live session is verified; never returns."""
    validate_request(request)
    channel.send(phase(request, "fault-armed"))
    require(
        canonical_digest(channel.receive()) == canonical_digest(phase(request, "fault-trigger")),
        "Controller fault trigger does not match its prepared challenge",
    )
    if request["name"] == "controller-exit":
        os._exit(37)
    if request["name"] == "transport-loss":
        channel.close()
    # Deliberately no runtime/channel heartbeat: only the external supervisor
    # and the independently configured systemd lifetime can stop this tree.
    while True:
        time.sleep(1)


def observe_fault(channel, service, request, *, started, deadline, on_tick):
    """Accept only this armed fault; do not stop the service to prove a timer."""
    validate_request(request)
    require(
        canonical_digest(channel.receive()) == canonical_digest(phase(request, "fault-armed")),
        "Controller fault was not armed with the current challenge",
    )
    on_tick()
    state = service.inspect()
    require(
        state is not None
        and state["ActiveState"] not in {"inactive", "failed"}
        and service.populated(),
        "Controller fault service stopped before the trigger",
    )
    require(
        type(service.runtime_limit) in {int, float}
        and math.isfinite(service.runtime_limit)
        and service.runtime_limit > 0,
        "Controller fault has no verified independent lifetime",
    )
    channel.send(phase(request, "fault-trigger"))
    # The normal exchange ends before the service's hard kill. This fault must
    # observe that kill, while preserving the separate storage recovery budget.
    channel.deadline = deadline - 1
    channel.expect_eof()
    disconnected = time.monotonic()
    if request["name"] == "service-deadline":
        require(
            disconnected >= started + service.runtime_limit - 0.1,
            "Controller disconnected before its independent hard deadline",
        )
    if request["name"] == "transport-loss":
        state = service.inspect()
        require(
            state is not None
            and state["ActiveState"] not in {"inactive", "failed"}
            and service.populated(),
            "Transport fault did not leave a live service for supervised cleanup",
        )
    else:
        stop_deadline = min(deadline - 1, time.monotonic() + STOP_SECONDS + 2)
        while True:
            on_tick()
            state = service.inspect()
            stopped = state is None or (
                state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0"
            )
            if stopped and not service.populated():
                break
            require(time.monotonic() < stop_deadline, "Controller fault left running processes")
            time.sleep(0.05)
    on_tick()
    return {
        "name": request["name"],
        "challenge": request["challenge"],
        "disconnect_seconds": disconnected - started,
        "independent_stop": request["name"] != "transport-loss",
    }
