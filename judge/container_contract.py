"""Trusted container supervision only; not a learner sandbox or GPU receipt."""

import hashlib
from pathlib import Path
from types import MappingProxyType

PROTOCOL = "praximesh-container-supervision-v1"
USER = "65532:65532"
CPU_QUOTA = 200000
CPU_PERIOD = 100000
MEMORY_BYTES = 2 * 1024**3
PIDS = 512
TMP_BYTES = 8 * 1024**2
STATE = "/run/praximesh"
ENTRY = "/opt/praximesh/container_entry.py"
PYTHON = "/usr/local/bin/python"
START_SECONDS = 5
STOP_SECONDS = 5
MAX_SECONDS = 180
STATUS_BYTES = 4096
SUPERVISION_PROFILE = "container-supervision-v1"
WORKLOAD_PROFILE = "container-workload-v1"
# Frozen whole-container budgets, including supervisor and runtime overhead.
# Existing IDs and values also describe persisted recovery records.
RESOURCE_PROFILES = MappingProxyType(
    {
        SUPERVISION_PROFILE: (CPU_QUOTA, CPU_PERIOD, MEMORY_BYTES, PIDS),
        WORKLOAD_PROFILE: (50000, 100000, 512 * 1024**2, 256),
    }
)


def resource_limits(profile=SUPERVISION_PROFILE):
    if type(profile) is not str or profile not in RESOURCE_PROFILES:
        raise ValueError("Unknown fixed container resource profile")
    quota, period, memory, pids = RESOURCE_PROFILES[profile]
    return {
        "cpu.max": f"{quota} {period}",
        "memory.max": str(memory),
        "memory.swap.max": "0",
        "pids.max": str(pids),
    }


def docker_resources(profile=SUPERVISION_PROFILE):
    resource_limits(profile)
    quota, period, memory, pids = RESOURCE_PROFILES[profile]
    return {
        "CpuQuota": quota,
        "CpuPeriod": period,
        "Memory": memory,
        "MemorySwap": memory,
        "PidsLimit": pids,
    }


def resource_marker(profile=SUPERVISION_PROFILE):
    resource_limits(profile)
    return "--" if profile == SUPERVISION_PROFILE else "--resource-profile=" + profile


def marker_profile(marker):
    for profile in RESOURCE_PROFILES:
        if marker == resource_marker(profile):
            return profile
    raise ValueError("Unknown fixed supervisor resource marker")


def source_digest():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("container_contract.py", "container_entry.py"):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()
