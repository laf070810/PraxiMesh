"""Original-container fault observation; no stop command can prove a timer."""

import math
import re
import time
from copy import deepcopy

from judge.container_contract import START_SECONDS, STOP_SECONDS
from judge.container_isolation_protocol import exact
from judge.controller_faults import FAULT_SECONDS, phase, require, validate_request
from judge.gpu_policy import PROFILE
from judge.sandbox import SandboxUnavailable

SCOPE = "container-controller-check-v1"
LABEL = "controller-fault"


def finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def check_budget(deadline):
    now = time.monotonic()
    minimum = (
        START_SECONDS + STOP_SECONDS + 5 + PROFILE["compile_seconds"] + PROFILE["request_seconds"]
    )
    if not finite(deadline) or not now + minimum < deadline <= now + FAULT_SECONDS:
        raise SandboxUnavailable("Insufficient bounded container fault and cleanup budget")


def armed_message(request):
    validate_request(request)
    return {
        **phase(request, "fault-armed"),
        "scope": SCOPE,
        "guest": {"id": LABEL, "correct": True, "status": "running", "attach_alive": True},
    }


def arm_fault(runtime, session, request):
    """Only after trusted comparison; confirm that exact original guest is live."""
    validate_request(request)
    original = runtime.containers[LABEL]
    require(
        original["process"] is session.process
        and session.compiled is True
        and runtime.inspect(LABEL)["status"] == "running"
        and session.process.poll() is None,
        "Controller fault guest stopped before arming",
    )
    return armed_message(request)


def validate_observation(report, request):
    validate_request(request)
    require(
        type(report) is dict
        and set(report)
        == {
            "scope",
            "name",
            "challenge",
            "container_id",
            "nonce",
            "hard_limit_seconds",
            "disconnect_seconds",
            "independent_stop",
            "armed",
            "termination",
        }
        and report["scope"] == SCOPE
        and report["name"] == request["name"]
        and report["challenge"] == request["challenge"]
        and type(report["container_id"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", report["container_id"])
        and type(report["nonce"]) is str
        and re.fullmatch(r"[a-f0-9]{32}", report["nonce"])
        and finite(report["hard_limit_seconds"])
        and 0 < report["hard_limit_seconds"] <= FAULT_SECONDS
        and finite(report["disconnect_seconds"])
        and 0 <= report["disconnect_seconds"] <= report["hard_limit_seconds"] + STOP_SECONDS
        and report["independent_stop"] is (request["name"] != "transport-loss")
        and exact(report["armed"], armed_message(request)),
        "Container controller fault observation is incomplete or mismatched",
    )
    state = report["termination"]
    require(
        type(state) is dict
        and set(state) == {"running", "pid", "exit_code", "oom_killed"}
        and type(state["pid"]) is int
        and type(state["exit_code"]) is int
        and state["oom_killed"] is False,
        "Controller fault did not observe its original termination state",
    )
    if request["name"] == "transport-loss":
        require(
            state["running"] is True and state["pid"] > 0 and state["exit_code"] == 0,
            "Transport fault did not leave its original container alive",
        )
    else:
        expected = 70 if request["name"] == "controller-exit" else 124
        require(
            state["running"] is False and state["pid"] == 0 and state["exit_code"] == expected,
            "Controller fault did not stop with its independent supervisor status",
        )
        if request["name"] == "service-deadline":
            require(
                report["disconnect_seconds"] >= report["hard_limit_seconds"] - 0.1,
                "Controller disconnected before its independent hard deadline",
            )


def validate_cleanup(value, *, container_id, nonce, device_uuid):
    require(
        exact(
            value,
            {
                "container": container_id,
                "removed": True,
                "ready": False,
                "history": device_uuid + "." + nonce + ".done.json",
            },
        ),
        "Controller fault cleanup does not match its original instance and journal",
    )


def validate_report(report, request, *, device_uuid, recovered=False):
    fields = {"cleanup"} | ({"recovery", "recovered"} if recovered else set())
    require(type(report) is dict and fields <= report.keys(), "Controller fault cleanup is missing")
    observation = {key: value for key, value in report.items() if key not in fields}
    validate_observation(observation, request)
    validate_cleanup(
        report["cleanup"],
        container_id=report["container_id"],
        nonce=report["nonce"],
        device_uuid=device_uuid,
    )
    if recovered:
        value = report["recovery"]
        require(
            report["recovered"] is True
            and type(value) is dict
            and set(value) == {"container_id", "nonce", "cleanup"}
            and type(value["container_id"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", value["container_id"])
            and type(value["nonce"]) is str
            and re.fullmatch(r"[a-f0-9]{32}", value["nonce"])
            and value["container_id"] != report["container_id"]
            and value["nonce"] != report["nonce"],
            "Controller fault did not recover in a new independent instance",
        )
        validate_cleanup(
            value["cleanup"],
            container_id=value["container_id"],
            nonce=value["nonce"],
            device_uuid=device_uuid,
        )


def observe_fault(connection, service, request, armed, *, started, deadline, on_tick):
    """Observe only an armed original task; caller performs journal cleanup later."""
    validate_request(request)
    require(exact(armed, armed_message(request)), "Controller fault was not correctly armed")
    channel = connection.channel
    hard = connection.deadline
    require(
        finite(started)
        and finite(hard)
        and finite(deadline)
        and 0 < hard - started <= FAULT_SECONDS
        and hard < deadline <= hard + STOP_SECONDS,
        "Controller fault observation cannot extend its original supervision budget",
    )

    def state():
        service.guard.assert_held()
        service.engine.check_socket()
        on_tick()
        value = service.inspect()
        require(
            value is not None
            and (value["id"], tuple(value["arguments"])) == connection.identity
            and float(value["arguments"][1]) == hard
            and value["oom_killed"] is False,
            "Controller fault lost its original instance or supervision identity",
        )
        require(time.monotonic() < deadline, "Controller fault observation deadline expired")
        return value

    before = state()
    require(
        before["running"] is True and before["pid"] > 0,
        "Controller stopped before its fault trigger",
    )
    channel.send(phase(request, "fault-trigger"))
    # This callback retains ownership/lease checks while allowing the expected
    # stopped state. It cannot start work or change the actual PID-1 deadline.
    channel.on_tick = state
    channel.deadline = deadline
    channel.next_tick = 0
    channel.expect_eof()
    disconnected = time.monotonic()
    observed = state()
    if request["name"] != "transport-loss":
        while observed["running"]:
            time.sleep(0.05)
            observed = state()
    report = {
        "scope": SCOPE,
        "name": request["name"],
        "challenge": request["challenge"],
        "container_id": before["id"],
        "nonce": before["arguments"][2],
        "hard_limit_seconds": hard - started,
        "disconnect_seconds": disconnected - started,
        "independent_stop": request["name"] != "transport-loss",
        "armed": deepcopy(armed),
        "termination": {
            key: observed[key] for key in ("running", "pid", "exit_code", "oom_killed")
        },
    }
    validate_observation(report, request)
    return report
