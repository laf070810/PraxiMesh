"""Versioned container GPU identity; saved evidence validation performs no IO."""

import re
from copy import deepcopy

from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_devices import validate_selection
from judge.driver_artifacts import validate_manifest as validate_driver
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_policy import PROFILE, PROFILES, profile_digest
from judge.gpu_task_protocol import CLEANUP_MARGIN, RECOVERY_MARGIN, validate_target
from judge.nvml_artifacts import validate_manifest as validate_management

BACKEND = "gvisor-gpu-container"
PROTOCOL = "praximesh-container-kernel-v1"
RESOURCE_SCOPE = "task-container-including-supervisor-v1"
FIELDS = frozenset(
    {
        "backend",
        "controller_protocol",
        "execution_digest",
        "target",
        "artifacts",
        "session_profile",
        "profile_digest",
        "workload_limits",
        "resource_profile",
        "resource_scope",
        "device_spec",
        "recovery_margin_seconds",
        "cleanup_margin_seconds",
        "controller_margin_seconds",
    }
)
ARTIFACT_FIELDS = frozenset(
    {
        "task_image",
        "task_contract",
        "runtime_contract",
        "controller_contract",
        "driver_manifest",
        "management_manifest",
        "runsc_sha256",
        "runsc_release",
        "nvrtc_archive_sha256",
        "nvrtc_libraries",
        "docker_cli_version",
        "docker_cli_sha256",
        "engine",
        "controller_limits",
    }
)
HEX64 = re.compile(r"[a-f0-9]{64}")
VERSION = re.compile(r"[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}(?:[-+][A-Za-z0-9_.-]{1,40})?")


def require(condition):
    if not condition:
        raise ValueError("Incomplete or incompatible container GPU environment")


def matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def validate_engine(value):
    require(type(value) is dict and set(value) == {"version", "kernel", "os", "architecture"})
    require(matches(VERSION, value["version"]))
    require(matches(r"[0-9][A-Za-z0-9._+-]{0,119}", value["kernel"]))
    require(value["os"] == "linux" and value["architecture"] == "amd64")
    return value


def engine_identity(info):
    require(type(info) is dict and info.get("Architecture") in {"x86_64", "amd64"})
    return validate_engine(
        {
            "version": info["ServerVersion"],
            "kernel": info["KernelVersion"],
            "os": info["OSType"],
            "architecture": "amd64",
        }
    )


def validate_environment(value, fingerprint):
    """Integrity of a trusted saved record, never permission to execute or publish ready."""
    require(type(value) is dict and set(value) == FIELDS)
    require(value["backend"] == BACKEND and value["controller_protocol"] == PROTOCOL)
    require(matches(HEX64, value["execution_digest"]) and matches(HEX64, fingerprint))
    validate_target(value["target"])
    selection = validate_selection(value["device_spec"])
    require(all(selection[key] == value["target"][key] for key in ("uuid", "driver")))
    saved = value["session_profile"]
    require(type(saved) is dict and type(saved.get("id")) is str)
    profile = PROFILES.get(saved["id"])
    require(profile is not None and canonical_digest(saved) == canonical_digest(profile))
    require(value["profile_digest"] == profile_digest(profile))
    # These v1 definitions describe historical records too. Add a new schema
    # for changed budgets or scopes; never reinterpret the existing version.
    require(value["resource_profile"] == "container-workload-v1")
    require(value["resource_scope"] == RESOURCE_SCOPE)
    require(
        value["workload_limits"]
        == {
            "cpu.max": "50000 100000",
            "memory.max": "536870912",
            "memory.swap.max": "0",
            "pids.max": "256",
        }
    )
    for name, expected in (
        ("recovery_margin_seconds", 35),
        ("cleanup_margin_seconds", 10),
        ("controller_margin_seconds", 5),
    ):
        require(type(value[name]) is int and value[name] == expected)
    artifacts = value["artifacts"]
    require(type(artifacts) is dict and set(artifacts) == ARTIFACT_FIELDS)
    require(matches(r"sha256:[a-f0-9]{64}", artifacts["task_image"]))
    for name in (
        "task_contract",
        "runtime_contract",
        "controller_contract",
        "runsc_sha256",
        "nvrtc_archive_sha256",
        "docker_cli_sha256",
    ):
        require(matches(HEX64, artifacts[name]))
    require(matches(r"release-[0-9]{8}\.[0-9]{1,8}", artifacts["runsc_release"]))
    require(matches(VERSION, artifacts["docker_cli_version"]))
    validate_engine(artifacts["engine"])
    driver = validate_driver(artifacts["driver_manifest"])
    management = validate_management(artifacts["management_manifest"])
    require(management["schema"] == 2)
    require(driver["driver_version"] == management["driver_version"] == selection["driver"])
    require(
        artifacts["controller_limits"]
        == {
            "cpu.max": "100000 100000",
            "memory.max": "536870912",
            "memory.swap.max": "0",
            "pids.max": "128",
        }
    )
    libraries = artifacts["nvrtc_libraries"]
    require(type(libraries) is dict and len(libraries) == 2)
    kinds = set()
    for name, digest in libraries.items():
        match = (
            re.fullmatch(r"libnvrtc(-builtins)?\.so\.[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}", name)
            if type(name) is str
            else None
        )
        require(match is not None and matches(HEX64, digest))
        kinds.add(match.group(1))
    require(kinds == {None, "-builtins"})
    require(canonical_digest(value) == fingerprint)
    return profile


def environment_identity(target, selection, artifacts, *, execution_digest):
    from judge.container_kernel_protocol import FINISH_MARGIN

    value = {
        "backend": BACKEND,
        "controller_protocol": PROTOCOL,
        "execution_digest": execution_digest,
        "target": deepcopy(target),
        "artifacts": deepcopy(artifacts),
        "session_profile": deepcopy(PROFILE),
        "profile_digest": profile_digest(),
        "device_spec": deepcopy(selection),
        "resource_profile": WORKLOAD_PROFILE,
        "resource_scope": RESOURCE_SCOPE,
        "workload_limits": resource_limits(WORKLOAD_PROFILE),
        "recovery_margin_seconds": RECOVERY_MARGIN,
        "cleanup_margin_seconds": CLEANUP_MARGIN,
        "controller_margin_seconds": FINISH_MARGIN,
    }
    validate_environment(value, canonical_digest(value))
    return value
