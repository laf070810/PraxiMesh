"""Fixed native PID probe inside an owned controller namespace, never user code."""

import errno
import json
import os
import signal
from pathlib import Path


def timeout(_signum, _frame):
    raise TimeoutError("Fixed PID probe exceeded its deadline")


def main():
    group = Path("/sys/fs/cgroup/workloads")
    if (
        os.getuid() != 0
        or Path("/proc/self/cgroup").read_text().strip() != "0::/manager"
        or (group / "pids.max").read_text().strip() != "256"
        or (group / "pids.current").read_text().strip() != "0"
    ):
        raise RuntimeError("PID probe requires the empty, bounded private workload cgroup")
    leaf = group / "pid-probe"
    leaf.mkdir()  # Do not enter or remove a pre-existing group.
    children = []
    limited = False
    try:
        (leaf / "cgroup.procs").write_text(str(os.getpid()))
        if Path("/proc/self/cgroup").read_text().strip() != "0::/workloads/pid-probe":
            raise RuntimeError("PID probe did not enter its owned workload cgroup")
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(5)
        for _index in range(384):
            try:
                pid = os.fork()
            except OSError as error:
                limited = error.errno == errno.EAGAIN
                break
            if pid == 0:
                signal.pause()
                os._exit(0)
            children.append(pid)
        result = {"event": "host-pids", "limited": limited, "children": len(children)}
    finally:
        signal.alarm(0)
        for pid in children:
            os.kill(pid, signal.SIGKILL)
        for pid in children:
            os.waitpid(pid, 0)
        (Path("/sys/fs/cgroup/manager") / "cgroup.procs").write_text(str(os.getpid()))
        leaf.rmdir()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
