"""Host controller environment identity; task data is shared across runtimes."""

import copy
import hashlib
import json
import re
from pathlib import Path

from judge import gpu_artifacts
from judge.gpu_namespace import WORKLOAD_LIMITS
from judge.gpu_policy import PROFILE, execution_digest, profile_digest
from judge.gpu_task_protocol import CLEANUP_MARGIN, RECOVERY_MARGIN, validate_target
from judge.gpu_task_protocol import PROBLEM_FIELDS as PROBLEM_FIELDS
from judge.gpu_task_protocol import finite_time as finite_time
from judge.gpu_task_protocol import task_request as task_request
from judge.gpu_task_protocol import validate_result as validate_result

PROTOCOL = 1
CONTROLLER_MARGIN = 12


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


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
