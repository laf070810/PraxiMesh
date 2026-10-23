"""Real execution only. Skips are an explicit release blocker, not a passing gate."""

import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.core.management import call_command

from judge.backends import configured_sandbox
from judge.evaluator import evaluate
from judge.sandbox import SandboxUnavailable

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.environ.get("RUN_SANDBOX_TESTS") != "1",
        reason="Requires explicitly configured real judge artifacts; skipped is not accepted",
    ),
]


def test_isolation_probes(settings, tmp_path):
    settings.JUDGE_RECEIPT = str(tmp_path / "receipt.json")
    call_command("sandbox_check")


def test_every_reference_and_constant_mutant(package):
    sandbox = configured_sandbox()
    environment = sandbox.preflight()
    for problem in package["problems"]:
        if (
            environment["policy"] == "gvisor-python-function-v1"
            and problem["evaluator"] != "python-function-v1"
        ):
            with pytest.raises(SandboxUnavailable):
                evaluate(problem, problem["reference"], "submit", sandbox)
            continue
        correct = evaluate(problem, problem["reference"], "submit", sandbox)
        assert correct["verdict"] == "accepted", problem["id"]
        source = (
            "function solve() { return null; }"
            if problem["language"] == "javascript"
            else "print('null')"
            if problem["evaluator"] == "python-stdio-v1"
            else f"def {problem['function']}(*args): return None"
        )
        mutant = evaluate(problem, source, "submit", sandbox)
        assert mutant["verdict"] == "wrong_answer", problem["id"]


def test_targeted_mistakes_and_forged_scores(package):
    sandbox = configured_sandbox()
    environment = sandbox.preflight()
    mistakes = json.loads(Path(__file__).with_name("incorrect_solutions.json").read_text())
    assert set(mistakes) == {problem["id"] for problem in package["problems"]}
    for problem in package["problems"]:
        if (
            environment["policy"] == "gvisor-python-function-v1"
            and problem["evaluator"] != "python-function-v1"
        ):
            continue  # The reference test above verifies explicit backend refusal.
        for mistake in mistakes[problem["id"]]:
            result = evaluate(problem, mistake["source"], "submit", sandbox)
            assert result["verdict"] in {"wrong_answer", "runtime_error"}, (
                problem["id"],
                mistake["mistake"],
                result,
            )
    forged = evaluate(
        package["problems"][0],
        'def solve(*args): return {"verdict":"accepted", "score":100}',
        "submit",
        sandbox,
    )
    assert forged["verdict"] == "wrong_answer"


def test_existing_host_canary_is_inaccessible(tmp_path):
    canary = tmp_path / "private-host-canary"
    canary.write_text("private host value not passed to the guest")
    sandbox = configured_sandbox()
    sandbox.preflight()
    result = sandbox.execute(
        "def solve(path):\n try:\n  open(path).read()\n  return False\n except OSError: return True",
        "solve",
        [str(canary)],
    )
    assert result.verdict == "ok" and result.value is True
    assert canary.read_text() == "private host value not passed to the guest"


@pytest.mark.skipif(
    os.environ.get("JUDGE_BACKEND") != "wasi", reason="WASI native driver lifecycle"
)
def test_worker_death_kills_real_sleeping_driver(tmp_path):
    # Trusted supervisor helper; the test program still runs only in WASI.
    helper = r"""
import json, os, sys
from pathlib import Path
from judge.backends import configured_sandbox
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
django.setup()
import judge.wasi as wasi
real_popen = wasi.subprocess.Popen
def tracked(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    Path(sys.argv[1]).write_text(str(child.pid))
    return child
wasi.subprocess.Popen = tracked
sandbox = configured_sandbox()
sandbox.preflight()
sandbox.execute("import time\ndef solve(): time.sleep(60)", "solve", [])
"""
    marker = tmp_path / "pid"
    environment = dict(os.environ, TMPDIR=str(tmp_path))
    parent = subprocess.Popen(
        [sys.executable, "-B", "-c", helper, str(marker)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    pidfd = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert parent.poll() is None, parent.stderr.read().decode()
            time.sleep(0.05)
        assert marker.exists()
        child_pid = int(marker.read_text())
        # uv's portable CPython may omit os.pidfd_open despite a supporting
        # Linux kernel. These are the x86_64 syscall numbers from unistd_64.h.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        pidfd = libc.syscall(434, child_pid, 0)
        assert pidfd >= 0, os.strerror(ctypes.get_errno())
        # Allow interpreter startup, then prove the sleeping process is alive.
        assert not select.select([pidfd], [], [], 1.5)[0]
        parent.kill()
        parent.wait(timeout=3)
        assert select.select([pidfd], [], [], 3)[0], "Driver survived supervisor death"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        parent.stderr.close()
        if pidfd is not None:
            if not select.select([pidfd], [], [], 0)[0]:
                assert libc.syscall(424, pidfd, signal.SIGKILL, 0, 0) == 0
            os.close(pidfd)
