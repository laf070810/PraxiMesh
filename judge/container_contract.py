"""Trusted container supervision only; not a learner sandbox or GPU receipt."""

import hashlib
from pathlib import Path

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


def source_digest():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("container_contract.py", "container_entry.py"):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()
