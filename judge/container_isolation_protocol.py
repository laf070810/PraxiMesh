"""Strict nested-runtime resource evidence, distinct from host Podman reports."""

import hashlib
import json
import math
import re

from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_devices import nodes, validate_selection
from judge.gpu_task_protocol import validate_target
from judge.sandbox import SandboxUnavailable

SCOPE = "container-resource-check-v1"
CHECKS = (
    "identity",
    "output",
    "timeout",
    "cancel",
    "client-exit",
    "cpu-limit",
    "storage-limit",
    "memory-limit",
    "pid-limit",
)
LABELS = tuple(
    label
    for name in CHECKS
    for label in ((name + "-recovery",) if name == "pid-limit" else (name, name + "-recovery"))
)


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def exact(left, right):
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def resource_request(canary):
    if type(canary) is not str or not re.fullmatch(r"[a-f0-9]{32}", canary):
        raise ValueError("Resource check requires a fresh bounded challenge")
    return {"event": "check-resources", "canary": canary}


def validate_request(value):
    if (
        type(value) is not dict
        or set(value) != {"event", "canary"}
        or value != resource_request(value["canary"])
    ):
        raise ValueError("Unexpected container resource request")
    return value


def target_identity(target, selection):
    if target is None and selection is None:
        return None, []
    validate_target(target)
    validate_selection(selection)
    require(
        target["uuid"] == selection["uuid"] and target["driver"] == selection["driver"],
        "Resource target differs from the original selected device",
    )
    return (
        {key: target[key] for key in ("uuid", "model", "architecture")} | {"count": 1},
        [
            {"path": path, "character": True, "major": major, "minor": minor}
            for path, (major, minor) in sorted(nodes(selection).items())
        ],
    )


def verify_identity(value, request, target, selection):
    cuda, devices = target_identity(target, selection)
    required_env = {"PATH", "PYTHONDONTWRITEBYTECODE", "HOME", "LANG"}
    if target is not None:
        required_env |= {"NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES"}
    require(
        type(value) is dict
        and set(value)
        == {
            "event",
            "uid",
            "root_write",
            "private_file_visible",
            "controller_loopback_reachable",
            "gpu_nodes",
            "cuda",
            "capabilities",
            "no_new_privileges",
            "environment_names",
            "sha256",
        },
        "Resource guest identity is incomplete",
    )
    names = value["environment_names"]
    require(
        value["event"] == "identity"
        and type(value["uid"]) is int
        and value["uid"] == 0
        and value["root_write"] is False
        and value["private_file_visible"] is False
        and value["controller_loopback_reachable"] is False
        and value["capabilities"] == "0000000000000000"
        and value["no_new_privileges"] == "1"
        and exact(value["gpu_nodes"], devices)
        and exact(value["cuda"], cuda)
        and type(names) is list
        and all(type(name) is str for name in names)
        and len(names) == len(set(names))
        and required_env <= set(names) <= required_env | {"LC_CTYPE"}
        and value["sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest(),
        "Resource guest escaped its expected identity or isolation",
    )


def positive_int(value):
    return type(value) is int and value > 0


def validate_details(name, details, request, target, selection):
    require(type(details) is dict, "Resource measurements are missing")
    valid = False
    if name == "identity":
        valid = set(details) == {"guest"}
        if valid:
            verify_identity(details["guest"], request, target, selection)
    elif name in {"output", "timeout", "memory-limit"}:
        verdict = {
            "output": "output_limit",
            "timeout": "time_limit",
            "memory-limit": "memory_limit",
        }[name]
        valid = details.get("verdict") == verdict and set(details) == (
            {"verdict", "oom_kills"} if name == "memory-limit" else {"verdict"}
        )
        if valid and name == "memory-limit":
            valid = positive_int(details["oom_kills"])
    elif name in {"cancel", "client-exit"}:
        valid = exact(
            details,
            {
                "interruption": name,
                "observation": {
                    "scope": "runsc-guest-v1",
                    "id": name,
                    "status": "running",
                    "attach_alive": True,
                },
            },
        )
    elif name == "cpu-limit":
        valid = set(details) == {"elapsed_seconds", "cpu_seconds", "throttled_periods"}
        if valid:
            elapsed, cpu = details["elapsed_seconds"], details["cpu_seconds"]
            valid = (
                all(type(n) in {int, float} and math.isfinite(n) for n in (elapsed, cpu))
                and elapsed >= 3
                and cpu >= 0.5
                and cpu / elapsed <= 0.7
                and positive_int(details["throttled_periods"])
            )
    elif name == "storage-limit":
        valid = (
            set(details) == {"event", "limited", "mib"}
            and details["event"] == "storage"
            and details["limited"] is True
            and positive_int(details["mib"])
            and details["mib"] <= 32
        )
    elif name == "pid-limit":
        valid = set(details) == {
            "limited",
            "children",
            "limit_events",
            "peak_tasks",
            "remaining_tasks",
            "initial_tasks",
        }
        if valid:
            valid = (
                details["limited"] is True
                and all(positive_int(details[key]) for key in set(details) - {"limited"})
                and details["children"] < 256
                and details["peak_tasks"] == 256
                and details["remaining_tasks"] == details["initial_tasks"] < 256
                and details["initial_tasks"] + details["children"] == 256
            )
    require(valid, "Resource check did not verify " + name)


def validate_report(report, request, *, target, selection):
    validate_request(request)
    target_identity(target, selection)
    require(
        type(report) is dict
        and set(report)
        == {
            "scope",
            "gpu",
            "canary_sha256",
            "resource_profile",
            "limits",
            "checks",
            "cleanup",
        }
        and report["scope"] == SCOPE
        and report["gpu"] == (None if target is None else target["uuid"])
        and report["resource_profile"] == WORKLOAD_PROFILE
        and exact(report["limits"], resource_limits(WORKLOAD_PROFILE))
        and report["canary_sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest()
        and type(report["checks"]) is list
        and len(report["checks"]) == len(CHECKS)
        and type(report["cleanup"]) is list
        and len(report["cleanup"]) == len(LABELS),
        "Container resource report differs from its original request or budget",
    )
    for name, row in zip(CHECKS, report["checks"], strict=True):
        require(
            type(row) is dict
            and set(row) == {"name", "passed", "cpu_recovery", "details"}
            and row["name"] == name
            and row["passed"] is True
            and row["cpu_recovery"] is True,
            "Resource check or its fresh recovery was not confirmed",
        )
        validate_details(name, row["details"], request, target, selection)
    for label, row in zip(LABELS, report["cleanup"], strict=True):
        require(
            type(row) is dict
            and set(row)
            == {"scope", "id", "running_before_cleanup", "exit_code", "stopped", "removed"}
            and row["scope"] == "runsc-guest-v1"
            and row["id"] == label
            and type(row["running_before_cleanup"]) is bool
            and type(row["exit_code"]) is int
            and row["stopped"] is True
            and row["removed"] is True,
            "Resource check did not confirm its original guest cleanup",
        )
