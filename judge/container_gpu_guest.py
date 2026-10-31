"""Fixed CPU-only node observation inside gVisor. Never opens an NVIDIA device."""

import json
import os
import stat
import sys
from pathlib import Path


def main():
    request = json.loads(sys.stdin.buffer.readline(4097))
    if request != {"protocol": 1, "operation": "inspect-device-nodes"}:
        raise ValueError("Unexpected fixed node observation request")
    found = {}
    for path in Path("/dev").glob("nvidia*"):
        info = path.lstat()
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError("Unexpected non-device NVIDIA entry")
        found[str(path)] = [os.major(info.st_rdev), os.minor(info.st_rdev)]
    print(
        json.dumps(
            {
                "event": "device-nodes",
                "nodes": found,
                "cuda_initialized": False,
                "uid": os.getuid(),
                "root_readonly": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
                "supervisor_visible": Path("/run/praximesh/status.json").exists(),
            }
        ),
        flush=True,
    )
    if sys.stdin.buffer.read(1):
        raise ValueError("Unexpected additional node observation input")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Only this fixed metadata probe uses this diagnostic; no learner source.
        print(
            json.dumps(
                {
                    "event": "node-error",
                    "failure_kind": type(error).__name__,
                    "message": str(error)[:160],
                }
            ),
            flush=True,
        )
        raise SystemExit(70) from None
