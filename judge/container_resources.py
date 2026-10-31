"""Kernel counters for fixed whole-container budgets; no learner input."""

import errno
import os
import signal
from pathlib import Path

from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.sandbox import SandboxUnavailable

GROUP = Path("/sys/fs/cgroup")
REQUIRED = {
    "cpu.stat": {"usage_usec", "nr_throttled", "throttled_usec"},
    "memory.events": {"max", "oom", "oom_kill"},
    "pids.events": {"max"},
}


def counters(name):
    if name not in REQUIRED:
        raise ValueError("Unknown kernel counter file")
    with (GROUP / name).open() as stream:
        data = stream.read(8193)
    if len(data) > 8192:
        raise SandboxUnavailable("Oversized kernel counters")
    result = {}
    try:
        for line in data.splitlines():
            key, value = line.split()
            if key in result or not value.isascii() or not value.isdecimal():
                raise ValueError
            result[key] = int(value)
        if not REQUIRED[name] <= result.keys():
            raise ValueError
    except (ValueError, TypeError):
        raise SandboxUnavailable("Incomplete or invalid kernel counters") from None
    return result


def host_pid_probe():
    """Fixed forks only in the owned private task cgroup, never arbitrary code."""
    expected = resource_limits(WORKLOAD_PROFILE)
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if (
        os.getuid() != 65532
        or os.getgid() != 65532
        or Path("/proc/self/cgroup").read_text().strip() != "0::/"
        or not os.statvfs("/").f_flag & os.ST_RDONLY
        or status["NoNewPrivs"].strip() != "1"
        or int(status["CapEff"].strip(), 16) != 0
        or any((GROUP / name).read_text().strip() != value for name, value in expected.items())
    ):
        raise SandboxUnavailable("PID probe requires the private bounded task container")
    before = counters("pids.events")["max"]
    children = []
    limited = False

    def timeout(_signum, _frame):
        raise TimeoutError("Fixed PID probe exceeded its deadline")

    previous = signal.signal(signal.SIGALRM, timeout)
    try:
        signal.alarm(8)
        for _index in range(384):
            try:
                pid = os.fork()
            except OSError as error:
                if error.errno != errno.EAGAIN:
                    raise
                limited = True
                break
            if pid == 0:
                signal.pause()
                os._exit(0)
            children.append(pid)
        events = counters("pids.events")["max"] - before
        peak = int((GROUP / "pids.current").read_text())
    finally:
        signal.alarm(0)
        for pid in children:
            os.kill(pid, signal.SIGKILL)
        for pid in children:
            os.waitpid(pid, 0)
        signal.signal(signal.SIGALRM, previous)
    if not limited or not 1 <= len(children) < 256 or events < 1 or peak != 256:
        raise SandboxUnavailable("Host PID limit did not stop the fixed probe")
    return {
        "limited": True,
        "children": len(children),
        "limit_events": events,
        "peak_tasks": peak,
        "remaining_tasks": int((GROUP / "pids.current").read_text()),
    }
