"""PID-1 deadline for fixed trusted controllers; never execute learner input."""

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# -I deliberately omits cwd/PYTHONPATH. Only the immutable sibling module is used.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from container_contract import (  # noqa: E402
    MAX_SECONDS,
    PROTOCOL,
    START_SECONDS,
    STATE,
    SUPERVISION_PROFILE,
    marker_profile,
    resource_limits,
    source_digest,
)


def arguments(values, now):
    if len(values) < 5:
        raise ValueError("Invalid supervisor arguments")
    marker_profile(values[3])
    start_before, deadline = float(values[0]), float(values[1])
    nonce, command = values[2], values[4:]
    if (
        not math.isfinite(start_before)
        or not math.isfinite(deadline)
        or not now < start_before <= now + START_SECONDS
        or not start_before < deadline <= now + MAX_SECONDS
        or not re.fullmatch("[a-f0-9]{32}", nonce)
        or not Path(command[0]).is_absolute()
        or any(not part or "\0" in part for part in command)
    ):
        raise ValueError("Invalid supervisor launch window")
    return start_before, deadline, nonce, command


def containment(profile=SUPERVISION_PROFILE):
    """Read enforced limits, not requested Docker metadata."""
    if os.getpid() != 1 or os.getuid() != 65532 or os.getgid() != 65532:
        raise ValueError("Expected the private non-root PID-1 supervisor")
    if Path("/proc/self/cgroup").read_text().strip() != "0::/":
        raise ValueError("Expected a private cgroup v2 namespace")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if status["NoNewPrivs"].strip() != "1" or int(status["CapEff"].strip(), 16) != 0:
        raise ValueError("Expected no privileges or capabilities")
    expected = resource_limits(profile)
    actual = {name: (Path("/sys/fs/cgroup") / name).read_text().strip() for name in expected}
    if actual != expected:
        raise ValueError("Container limits were not applied")
    return actual


def reap_children(child):
    """PID 1 also owns orphaned runtime helpers; preserve the main child's status."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)


def main(values=None):
    values = sys.argv[1:] if values is None else values
    try:
        before, deadline, nonce, command = arguments(values, time.monotonic())
        limits = containment(marker_profile(values[3]))
        if time.monotonic() >= before:
            return 70
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, stop)
        # Do not inherit Docker-client proxy settings or application credentials.
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": STATE,
        }
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        path = Path(STATE) / "status.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {
                    "protocol": PROTOCOL,
                    "source_digest": source_digest(),
                    "nonce": nonce,
                    "deadline": deadline,
                    "child": child.pid,
                    "limits": limits,
                },
                stream,
            )
        while not stopping:
            if time.monotonic() >= deadline:
                return 124
            reap_children(child)
            result = child.poll()
            if result is not None:
                return 0 if result == 0 else 70
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return 143
    except Exception:
        # No raw exception, executable arguments, child output or private data.
        return 70


if __name__ == "__main__":
    # Exiting PID 1 also terminates detached descendants in this PID namespace.
    os._exit(main())
