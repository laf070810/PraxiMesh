"""Read-only GPU evidence from trusted persisted worker records, never uploads."""

import copy
import re
import uuid

from core.resources import GPU_BUDGET_POLICY, reservation_units
from judge.gpu_contract import EVALUATOR, LANGUAGE
from judge.gpu_execution_contract import canonical_digest, validate_result, validate_target
from judge.gpu_policy import PROFILES, profile_digest
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

HEX64 = re.compile(r"[a-f0-9]{64}")
SHA256 = re.compile(r"sha256:[a-f0-9]{64}")
VERSION = re.compile(r"[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}")
ENVIRONMENT_FIELDS = frozenset(
    {
        "backend",
        "controller_protocol",
        "execution_digest",
        "target",
        "artifacts",
        "session_profile",
        "profile_digest",
        "workload_limits",
        "recovery_margin_seconds",
        "cleanup_margin_seconds",
        "controller_margin_seconds",
    }
)
ARTIFACT_FIELDS = frozenset(
    {
        "image_archive_sha256",
        "image_manifest",
        "image_id",
        "runsc_sha256",
        "runsc_release",
        "podman_version",
        "nvrtc_archive_sha256",
        "nvrtc_libraries",
        "harness_sha256",
    }
)
HARNESS_FIELDS = frozenset({"__init__.py", "gpu_contract.py", "gpu_runtime.py", "results.py"})


def require(condition):
    if not condition:
        raise ValueError("Incomplete or incompatible GPU result evidence")


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def environment_profile(environment, fingerprint):
    """Validate saved identity without comparing historical hashes to live files.

    This verifies a trusted record's integrity and comparability, not physical
    runtime readiness. The worker's private receipt provides the latter gate.
    """
    require(
        type(environment) is dict
        and set(environment) == ENVIRONMENT_FIELDS | {"language", "evaluator"}
    )
    require(environment["language"] == LANGUAGE and environment["evaluator"] == EVALUATOR)
    require(
        environment["backend"] == "gvisor-gpu"
        and type(environment["controller_protocol"]) is int
        and environment["controller_protocol"] == 1
    )
    require(_matches(HEX64, environment["execution_digest"]) and _matches(HEX64, fingerprint))
    validate_target(environment["target"])
    saved_profile = environment["session_profile"]
    require(type(saved_profile) is dict and type(saved_profile.get("id")) is str)
    profile = PROFILES.get(saved_profile["id"])
    require(profile is not None and canonical_digest(saved_profile) == canonical_digest(profile))
    require(environment["profile_digest"] == profile_digest(profile))
    for name in ("recovery_margin_seconds", "cleanup_margin_seconds", "controller_margin_seconds"):
        require(type(environment[name]) is int and 1 <= environment[name] <= 180)
    limits = environment["workload_limits"]
    require(
        type(limits) is dict
        and set(limits) == {"cpu.max", "memory.max", "memory.swap.max", "pids.max"}
    )
    require(_matches(re.compile(r"[1-9][0-9]{0,8} [1-9][0-9]{0,8}"), limits["cpu.max"]))
    require(limits["memory.swap.max"] == "0")
    for name in ("memory.max", "pids.max"):
        require(_matches(re.compile(r"[1-9][0-9]{0,12}"), limits[name]))
    artifacts = environment["artifacts"]
    require(type(artifacts) is dict and set(artifacts) == ARTIFACT_FIELDS)
    for name in ("image_archive_sha256", "runsc_sha256", "nvrtc_archive_sha256"):
        require(_matches(HEX64, artifacts[name]))
    for name in ("image_manifest", "image_id"):
        require(_matches(SHA256, artifacts[name]))
    require(_matches(VERSION, artifacts["podman_version"]))
    require(_matches(re.compile(r"release-[0-9]{8}\.[0-9]{1,8}"), artifacts["runsc_release"]))
    harness = artifacts["harness_sha256"]
    require(
        type(harness) is dict
        and set(harness) == HARNESS_FIELDS
        and all(_matches(HEX64, value) for value in harness.values())
    )
    libraries = artifacts["nvrtc_libraries"]
    require(type(libraries) is dict and len(libraries) == 2)
    kinds = set()
    for name, value in libraries.items():
        match = (
            re.fullmatch(r"libnvrtc(-builtins)?\.so\.([0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8})", name)
            if type(name) is str
            else None
        )
        require(match is not None and _matches(HEX64, value))
        kinds.add(match.group(1))
    require(kinds == {None, "-builtins"})
    require(
        canonical_digest({name: environment[name] for name in ENVIRONMENT_FIELDS}) == fingerprint
    )
    return profile


def public_feedback(version, result, mode, *, profile=None):
    """No hidden cases, raw logs, unknown nested fields or unvalidated timings."""
    try:
        require(
            type(version.data) is dict
            and version.data.get("evaluator") == EVALUATOR
            and version.data.get("language") == LANGUAGE
        )
        require(mode in {"run", "submit"})
        cases = version.data.get("tests")
        require(type(cases) is list and 2 <= len(cases) <= 8)
        require(
            all(
                type(case) is dict
                and type(case.get("name")) is str
                and type(case.get("public")) is bool
                for case in cases
            )
        )
        require(
            len({case["name"] for case in cases}) == len(cases)
            and {case["public"] for case in cases} == {False, True}
        )
        validate_result(result, version.data, mode, profile=profile)
        require(result["verdict"] in LABELS and result["verdict"] != "internal_error")
        # Incorrect repeated batches may follow fully passed correctness cases.
        # Non-accepted results must never project a performance score.
        return copy.deepcopy(result)
    except (SandboxUnavailable, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("Invalid GPU public feedback") from None


def verified(job):
    require(
        job.mode == "submit"
        and job.status == "finished"
        and job.verdict in set(LABELS) - {"internal_error"}
    )
    require(
        job.started_at is not None
        and job.finished_at is not None
        and job.execution_started_at is not None
    )
    require(job.created_at <= job.started_at <= job.execution_started_at <= job.finished_at)
    require(
        job.budget_policy == GPU_BUDGET_POLICY
        and job.budget_state == "consumed"
        and job.budget_units == reservation_units(job.version, "submit")
    )
    require(
        job.lease_until is None
        and job.lease_token is not None
        and _matches(HEX64, job.source_digest)
    )
    uuid.UUID(str(job.lease_token))
    profile = environment_profile(job.environment, job.evaluation_fingerprint)
    result = public_feedback(job.version, job.result, job.mode, profile=profile)
    require(result["verdict"] == job.verdict)
    return {
        "passed": result["passed"],
        "total": result["total"],
        "verdict": job.verdict,
        "assessment": {
            "kind": EVALUATOR,
            "resource_profile": profile["id"],
            "profile_digest": profile_digest(profile),
            "measurement": profile["measurement"],
            "unit": "ms",
        },
    }
