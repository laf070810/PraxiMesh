"""Trusted controller messages and environment identity; no admission receipt."""

import copy
import hashlib
import json
import math
import re
import statistics
from pathlib import Path

from judge import gpu_artifacts
from judge.gpu_contract import compile_request, validate_problem
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_namespace import WORKLOAD_LIMITS
from judge.gpu_policy import PROFILE, execution_digest, profile_digest
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

PROTOCOL = 1
CONTROLLER_MARGIN = 12
RECOVERY_MARGIN = 35
CLEANUP_MARGIN = 10
PROBLEM_FIELDS = ("evaluator", "language", "operation", "entrypoint", "comparison", "tests")


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


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


def expected_identity(image_archive, podman_version):
    if type(podman_version) is not str or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", podman_version
    ):
        raise ValueError("An explicit Podman version is required")
    root = Path(__file__).resolve().parents[1]
    return {
        **gpu_artifacts.verify_image_archive(image_archive),
        "runsc_sha256": gpu_artifacts.RUNSC_SHA256,
        "runsc_release": gpu_artifacts.RELEASE,
        "podman_version": podman_version,
        "nvrtc_archive_sha256": gpu_artifacts.NVRTC_ARCHIVE_SHA256,
        "nvrtc_libraries": dict(gpu_artifacts.LIBRARIES),
        "harness_sha256": {
            name: gpu_artifacts.sha256(root / "judge" / name) for name in gpu_artifacts.HARNESS
        },
    }


def environment_identity(target, identity):
    validate_target(target)
    return {
        "backend": "gvisor-gpu",
        "controller_protocol": PROTOCOL,
        "execution_digest": execution_digest(),
        "target": copy.deepcopy(target),
        "artifacts": copy.deepcopy(identity),
        "session_profile": copy.deepcopy(PROFILE),
        "profile_digest": profile_digest(),
        "workload_limits": dict(WORKLOAD_LIMITS),
        "recovery_margin_seconds": RECOVERY_MARGIN,
        "cleanup_margin_seconds": CLEANUP_MARGIN,
        "controller_margin_seconds": CONTROLLER_MARGIN,
    }


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


def validate_result(result, problem, mode):
    """Even trusted IPC returns only bounded learner-visible result fields."""
    required = {"verdict", "label", "passed", "total", "public_cases"}
    allowed = required | {"compile_initialization_ms", "performance"}
    total = sum(mode == "submit" or case["public"] for case in problem["tests"])
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
    performance = result.get("performance")
    if mode != "submit" or result["verdict"] != "accepted":
        if performance is not None:
            raise SandboxUnavailable("GPU performance requires a fully correct Submit")
        return
    expected = {
        "profile": PROFILE["id"],
        "profile_digest": profile_digest(),
        "measurement": PROFILE["measurement"],
        "warmup_batches": PROFILE["warmup_batches"],
        "sample_batches": PROFILE["sample_batches"],
        "batch_repetitions": PROFILE["batch_repetitions"],
    }
    if (
        type(performance) is not dict
        or set(performance) != set(expected) | {"samples_ms", "median_batch_ms"}
        or any(
            type(performance[name]) is not type(value) or performance[name] != value
            for name, value in expected.items()
        )
        or type(performance["samples_ms"]) is not list
        or len(performance["samples_ms"]) != PROFILE["sample_batches"]
        or not all(finite_time(value) for value in performance["samples_ms"])
        or not finite_time(performance["median_batch_ms"])
        or performance["median_batch_ms"] != statistics.median(performance["samples_ms"])
    ):
        raise SandboxUnavailable("Invalid GPU controller performance samples")


def finite_time(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 180_000
