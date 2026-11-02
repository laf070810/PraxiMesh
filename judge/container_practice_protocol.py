"""One original local operation, never a server result or readiness protocol."""

import math
import time
from copy import deepcopy

from judge.container_adapter import ContainerGPUAdapter
from judge.container_contract import MAX_SECONDS
from judge.container_controller import ContainerController, controller_digest
from judge.container_environment import validate_environment
from judge.container_journal import NONCE
from judge.container_practice import execute_public
from judge.container_practice_state import LocalPracticeState, require
from judge.container_service import HEX_ID, IMAGE_ID
from judge.driver_artifacts import validate_manifest
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_policy import execution_digest
from judge.gpu_receipt import CHECKS, SUITE_SECONDS
from judge.gpu_task_protocol import public_task_request, validate_result

PROTOCOL = "praximesh-local-gpu-control-v1"
ENTRY = "/opt/praximesh/scripts/container_practice_entry.py"
MODES = {"initialize", "run", "recover"}
MAX_SECONDS_TOTAL = SUITE_SECONDS + MAX_SECONDS + 30
PUBLIC_SCOPE = "Local public examples only; no server progress or performance score."


def same(value, expected):
    # Python equality treats False as 0 and True as 1; wire identities must not.
    return canonical_digest(value) == canonical_digest(expected)


def invocation(mode, device, nonce, deadline):
    if (
        type(mode) is not str
        or mode not in MODES
        or type(device) is not str
        or not GPU_UUID.fullmatch(device)
        or type(nonce) is not str
        or not NONCE.fullmatch(nonce)
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or not 0 < deadline - time.monotonic() <= MAX_SECONDS_TOTAL
    ):
        raise ValueError("An explicit bounded local invocation is required")


def hello(mode, device, nonce, deadline, binding):
    invocation(mode, device, nonce, deadline)
    require(type(binding) is str and HEX_ID.fullmatch(binding), "Invalid original local binding")
    return {
        "event": "local-ready",
        "protocol": PROTOCOL,
        "mode": mode,
        "device": device,
        "nonce": nonce,
        "deadline": deadline,
        "binding": binding,
        "controller_contract": controller_digest(),
        "execution_digest": execution_digest(),
    }


def configuration(image, driver):
    if type(image) is not str or not IMAGE_ID.fullmatch(image):
        raise ValueError("An immutable task image is required")
    return {"event": "prepare-local", "image": image, "driver": deepcopy(validate_manifest(driver))}


def checked_result(value, problem, environment):
    require(
        type(value) is dict
        and set(value)
        == {
            "scope",
            "formal_result",
            "result",
            "environment",
            "ready",
            "startup_checks",
            "startup_evidence_digest",
        }
        and value["scope"] == PUBLIC_SCOPE
        and value["formal_result"] is False
        and value["ready"] is False
        and type(value["startup_checks"]) is int
        and value["startup_checks"] == len(CHECKS)
        and type(value["startup_evidence_digest"]) is str
        and HEX_ID.fullmatch(value["startup_evidence_digest"])
        and same(value["environment"], environment),
        "Invalid local public completion",
    )
    validate_environment(environment, canonical_digest(environment))
    validate_result(value["result"], problem, "run")
    require("performance" not in value["result"], "Local examples cannot contain a score")
    return deepcopy(value)


def serve(control, state, channel, *, mode, nonce, on_tick):
    """Called only by the fixed entry after original mounts and ownership checks."""
    require(
        type(control) is ContainerController
        and type(state) is LocalPracticeState
        and state.journal is control.journal
        and state.guard is control.guard,
        "The original local controller and state are required",
    )
    device = state.guard.device_uuid
    binding = canonical_digest(state.marker["binding"])
    greeting = hello(mode, device, nonce, channel.deadline, binding)

    def check():
        on_tick()
        control.check()
        state.check()
        require(time.monotonic() < channel.deadline, "Local invocation deadline expired")

    def tick():
        check()
        channel.connected()

    check()
    channel.send(greeting)
    message = channel.receive()
    if mode == "run":
        require(
            type(message) is dict
            and set(message) == {"event", "image", "driver"}
            and message["event"] == "prepare-local",
            "Expected explicit local task preparation",
        )
        config = configuration(message["image"], message["driver"])
        previous = state.operation()
        require(
            state.journal.load() is None and (previous is None or previous["phase"] != "pending"),
            "Recover the original local operation before preparation",
        )
        tick()
        adapter = ContainerGPUAdapter(
            control, image=config["image"], driver_manifest=config["driver"]
        )
        adapter.check()
        tick()
        environment = deepcopy(adapter.environment)
        channel.send({"event": "local-prepared", "nonce": nonce, "environment": environment})
        message = channel.receive()
        require(
            type(message) is dict
            and set(message) == {"event", "mode", "problem", "source"}
            and message["event"] == "execute-public"
            and message["mode"] == "run",
            "Expected public examples after local preparation",
        )
        request = public_task_request(message["problem"], message["source"])
        progress = []

        def advanced(name):
            tick()
            require(
                len(progress) < len(CHECKS) and name == CHECKS[len(progress)],
                "Invalid startup order",
            )
            progress.append(name)
            channel.send(
                {"event": "local-progress", "nonce": nonce, "index": len(progress), "name": name}
            )

        result = execute_public(
            adapter,
            state,
            request["problem"],
            request["source"],
            on_tick=tick,
            on_progress=advanced,
        )
        require(tuple(progress) == CHECKS, "Local startup did not report all checks")
        result = checked_result(result, request["problem"], environment)
    else:
        require(message == {"event": mode}, "Unexpected local maintenance request")
        tick()
        result = {"status": "initialized", "ready": False, "formal_result": False}
        if mode == "recover":
            recovered = state.recover()
            require(
                recovered["ready"] is False and recovered["formal_result"] is False,
                "Invalid recovery scope",
            )
            result["status"] = recovered["status"]
    tick()
    channel.send({"event": "local-completed", "nonce": nonce, "result": result})
    tick()
    return result


def exchange(
    channel,
    expected,
    *,
    image=None,
    driver=None,
    environment=None,
    problem=None,
    source=None,
    on_progress=None,
):
    """Launcher must additionally wait for the original container's successful exit."""
    mode, nonce = expected["mode"], expected["nonce"]
    require(
        same(
            expected, hello(mode, expected["device"], nonce, channel.deadline, expected["binding"])
        ),
        "Invalid expected local handshake",
    )
    request = config = None
    if mode == "run":
        request = public_task_request(problem, source)
        config = configuration(image, driver)
        validate_environment(environment, canonical_digest(environment))
        require(
            environment["target"]["uuid"] == expected["device"]
            and environment["artifacts"]["task_image"] == image
            and environment["artifacts"]["driver_manifest"] == config["driver"]
            and environment["artifacts"]["controller_contract"] == expected["controller_contract"]
            and environment["execution_digest"] == expected["execution_digest"],
            "Local expected environment differs from the original invocation",
        )
    else:
        require(
            all(value is None for value in (image, driver, environment, problem, source)),
            "Maintenance cannot carry a task",
        )
    require(same(channel.receive(), expected), "Local controller startup identity mismatch")
    channel.on_tick()
    if mode == "run":
        channel.send(config)
        require(
            same(
                channel.receive(),
                {"event": "local-prepared", "nonce": nonce, "environment": environment},
            ),
            "Local controller environment mismatch; source withheld",
        )
        channel.on_tick()
        channel.send(request)
        for index, name in enumerate(CHECKS, 1):
            require(
                same(
                    channel.receive(),
                    {"event": "local-progress", "nonce": nonce, "index": index, "name": name},
                ),
                "Incomplete or reordered local startup progress",
            )
            if on_progress is not None:
                on_progress(name)
    else:
        channel.send({"event": mode})
    completed = channel.receive()
    require(
        type(completed) is dict
        and set(completed) == {"event", "nonce", "result"}
        and completed["event"] == "local-completed"
        and completed["nonce"] == nonce,
        "Invalid local completion identity",
    )
    result = completed["result"]
    if mode == "run":
        result = checked_result(result, request["problem"], environment)
    else:
        require(
            type(result) is dict
            and set(result) == {"status", "ready", "formal_result"}
            and result["ready"] is False
            and result["formal_result"] is False
            and type(result["status"]) is str
            and result["status"]
            in ({"initialized"} if mode == "initialize" else {"idle", "recovered"}),
            "Invalid local maintenance completion",
        )
    channel.expect_eof()
    channel.on_tick()
    return deepcopy(result)
