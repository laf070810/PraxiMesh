"""Fixed CPU process trees for supervision acceptance; no learner or GPU input."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE = Path("/run/praximesh")


def save(name, data):
    temporary = STATE / (name + ".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(STATE / (name + ".json"))


def main():
    mode = sys.argv[1]
    if mode not in {"tree", "exit-parent", "busy", "leaf", "busy-leaf"}:
        return 70
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode in {"leaf", "busy-leaf"}:
        if mode == "leaf":
            save("leaf", {"pid": os.getpid(), "parent": os.getppid(), "session": os.getsid(0)})
        while True:
            if mode == "leaf":
                time.sleep(0.1)
            else:
                sum(number * number for number in range(10000))
    children = [
        subprocess.Popen(
            [sys.executable, "-I", __file__, "busy-leaf" if mode == "busy" else "leaf"],
            start_new_session=True,
        )
        for _ in range(4 if mode == "busy" else 1)
    ]
    save(
        "tree",
        {
            "pid": os.getpid(),
            "children": [child.pid for child in children],
            "environment": sorted(os.environ),
        },
    )
    if mode == "busy":
        before = (Path("/sys/fs/cgroup") / "cpu.stat").read_text()
        time.sleep(2)
        after = (Path("/sys/fs/cgroup") / "cpu.stat").read_text()
        save("throttle", {"before": before, "after": after})
    if mode == "exit-parent":
        time.sleep(5)
        return 0
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
