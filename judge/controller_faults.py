"""Fixed trusted-controller fault handshake, independent of the host runtime."""

import os
import re
import time

from judge.sandbox import SandboxUnavailable

CHECKS = ("controller-exit", "transport-loss", "service-deadline")
FAULT_SECONDS = 90
RECOVERY_SECONDS = 90


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def fault_request(name, challenge):
    if (
        type(name) is not str
        or name not in CHECKS
        or type(challenge) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", challenge)
    ):
        raise ValueError("A fixed controller fault and fresh challenge are required")
    return {"event": "check-controller", "name": name, "challenge": challenge}


def validate_request(request):
    require(
        type(request) is dict
        and set(request) == {"event", "name", "challenge"}
        and request == fault_request(request["name"], request["challenge"]),
        "Unexpected controller fault request",
    )
    return request


def phase(request, event):
    return {**request, "event": event}


def trigger_fault(channel, request, *, armed=None):
    """Trusted caller must verify a live correct guest first; never returns."""
    validate_request(request)
    channel.send(phase(request, "fault-armed") if armed is None else armed)
    require(
        channel.receive() == phase(request, "fault-trigger"),
        "Controller fault trigger does not match its prepared challenge",
    )
    if request["name"] == "controller-exit":
        os._exit(37)
    if request["name"] == "transport-loss":
        channel.close()
    # No runtime/channel heartbeat: only independent supervision stops this tree.
    while True:
        time.sleep(1)
