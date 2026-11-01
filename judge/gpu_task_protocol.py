"""Shared bounded GPU task data; independent of host or container supervision."""

import copy
import math
import re
import statistics

from judge.gpu_contract import compile_request, validate_problem
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_policy import PROFILE, profile_digest
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

CLEANUP_MARGIN = 10
RECOVERY_MARGIN = 35
PROBLEM_FIELDS = ("evaluator", "language", "operation", "entrypoint", "comparison", "tests")


def validate_target(target):
    if (
        type(target) is not dict
        or set(target) != {"index", "uuid", "model", "driver", "architecture"}
        or type(target["index"]) is not int
        or target["index"] < 0
        or type(target["uuid"]) is not str
        or not GPU_UUID.fullmatch(target["uuid"])
        or type(target["model"]) is not str
        or not 1 <= len(target["model"]) <= 120
        or type(target["driver"]) is not str
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", target["driver"])
        or type(target["architecture"]) is not list
        or len(target["architecture"]) != 2
        or any(
            type(number) is not int or not 0 <= number <= 100 for number in target["architecture"]
        )
    ):
        raise ValueError("An exact single-device identity is required")


def task_request(problem, source, mode):
    validate_problem(problem)
    compile_request(source, problem["operation"])
    if mode not in {"run", "submit"}:
        raise ValueError("Unknown GPU task mode")
    return {
        "event": "execute",
        "mode": mode,
        "source": source,
        "problem": {name: copy.deepcopy(problem[name]) for name in PROBLEM_FIELDS},
    }


def public_task_request(problem, source):
    """Local examples cannot become Submit or smuggle private/execution fields."""
    validate_problem(problem, public_only=True)
    if set(problem) != set(PROBLEM_FIELDS):
        raise ValueError("A public GPU task must contain only its execution fields")
    compile_request(source, problem["operation"])
    return {
        "event": "execute-public",
        "mode": "run",
        "source": source,
        "problem": copy.deepcopy(problem),
    }


def validate_result(result, problem, mode, *, profile=None):
    """Even trusted IPC returns only bounded learner-visible result fields."""
    profile = PROFILE if profile is None else profile
    required = {"verdict", "label", "passed", "total", "public_cases"}
    allowed = required | {"compile_initialization_ms", "performance"}
    selected = [case for case in problem["tests"] if mode == "submit" or case["public"]]
    total = len(selected)
    if (
        type(result) is not dict
        or not required <= set(result) <= allowed
        or type(result.get("verdict")) is not str
        or result.get("verdict") not in LABELS
        or result.get("verdict") == "internal_error"
        or result.get("label") != LABELS[result["verdict"]]
        or type(result["passed"]) is not int
        or not 0 <= result["passed"] <= total
        or type(result["total"]) is not int
        or result["total"] != total
        or type(result["public_cases"]) is not list
    ):
        raise SandboxUnavailable("Invalid GPU controller result")
    public = [case["name"] for case in problem["tests"] if case["public"]]
    if len(result["public_cases"]) > len(public):
        raise SandboxUnavailable("Invalid GPU public feedback")
    for case, name in zip(result["public_cases"], public, strict=False):
        if (
            type(case) is not dict
            or set(case) != {"name", "verdict", "label"}
            or case["name"] != name
            or case["verdict"] not in {"accepted", "wrong_answer"}
            or case["label"] != LABELS[case["verdict"]]
        ):
            raise SandboxUnavailable("Invalid GPU public feedback")
    visible_positions = [index for index, case in enumerate(selected) if case["public"]]
    maximum_reached = (
        visible_positions[len(result["public_cases"])]
        if len(result["public_cases"]) < len(visible_positions)
        else total
    )
    public_passed = sum(case["verdict"] == "accepted" for case in result["public_cases"])
    public_failed = len(result["public_cases"]) - public_passed
    if not public_passed <= result["passed"] <= maximum_reached - public_failed or (
        result["verdict"] == "wrong_answer" and len(result["public_cases"]) != len(public)
    ):
        raise SandboxUnavailable("Contradictory GPU public case counts")
    if result["verdict"] == "accepted" and (
        result["passed"] != total
        or len(result["public_cases"]) != len(public)
        or any(case["verdict"] != "accepted" for case in result["public_cases"])
    ):
        raise SandboxUnavailable("Incomplete accepted GPU result")
    if "compile_initialization_ms" in result and not finite_time(
        result["compile_initialization_ms"]
    ):
        raise SandboxUnavailable("Invalid GPU compile timing")
    if result["verdict"] == "accepted" and result.get("compile_initialization_ms", 0) <= 0:
        raise SandboxUnavailable("Accepted GPU result requires completed compilation")
    performance = result.get("performance")
    if mode != "submit" or result["verdict"] != "accepted":
        if "performance" in result:
            raise SandboxUnavailable("GPU performance requires a fully correct Submit")
        return
    expected = {
        "profile": profile["id"],
        "profile_digest": profile_digest(profile),
        "measurement": profile["measurement"],
        "warmup_batches": profile["warmup_batches"],
        "sample_batches": profile["sample_batches"],
        "batch_repetitions": profile["batch_repetitions"],
    }
    if (
        type(performance) is not dict
        or set(performance) != set(expected) | {"samples_ms", "median_batch_ms"}
        or any(
            type(performance[name]) is not type(value) or performance[name] != value
            for name, value in expected.items()
        )
        or type(performance["samples_ms"]) is not list
        or len(performance["samples_ms"]) != profile["sample_batches"]
        or not all(finite_time(value) and value > 0 for value in performance["samples_ms"])
        or not finite_time(performance["median_batch_ms"])
        or performance["median_batch_ms"] != statistics.median(performance["samples_ms"])
    ):
        raise SandboxUnavailable("GPU performance requires complete positive controller samples")


def finite_time(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 180_000
