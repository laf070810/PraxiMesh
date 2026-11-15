"""Actual CPU process handles plus bounded current-query ownership checks; no GPU."""

import errno
import io
import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge.container_processes import (
    HostProcessView,
    OwnedProcesses,
    ProcessDisappeared,
    SampledProcess,
)
from judge.sandbox import SandboxUnavailable
from tests.test_container_bridge_exit import pidfds  # noqa: F401


@pytest.fixture
def live_sample(monkeypatch, pidfds):  # noqa: F811
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
    )
    view = object.__new__(HostProcessView)
    view.fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY)
    # The read-only mount contract has separate container checks. Here the
    # process fields, pidfd, fdinfo, exit and reaping are real Linux operations.
    monkeypatch.setattr(view, "check", lambda: None)
    group = Path(f"/proc/{process.pid}/cgroup").read_text().strip()
    try:
        yield SimpleNamespace(process=process, view=view, group=group)
    finally:
        process.stdin.close()
        process.wait(timeout=3)
        view.close()


def reap(item):
    item.process.stdin.close()
    assert item.process.wait(timeout=3) == 0


def test_current_sample_handle_confirms_real_exit_only_at_final_fence(live_sample):
    item = live_sample
    sampled = SampledProcess(item.view, item.process.pid, item.group)
    try:
        sampled.check()
        sampled.check(final=True)
        reap(item)
        assert not Path(f"/proc/{item.process.pid}").exists()
        with pytest.raises(ProcessDisappeared):
            sampled.check()  # A later positive sample cannot reuse this binding.
        sampled.check(final=True)
    finally:
        sampled.close()
        sampled.close()


@pytest.mark.parametrize("defect", ["missing-live", "permission", "reused", "moved", "view"])
def test_process_handle_never_hides_missing_live_or_changed_identity(
    live_sample, monkeypatch, defect
):
    item = live_sample
    sampled = SampledProcess(item.view, item.process.pid, item.group)
    original = deepcopy(sampled.identity)
    if defect != "missing-live":
        reap(item)

    def read(_pid):
        if defect == "missing-live":
            raise ProcessDisappeared("Modeled metadata gap while original CPU process lives")
        if defect == "permission":
            raise SandboxUnavailable("Host process cannot be read")
        value = deepcopy(original)
        if defect == "reused":
            value["birth"] += 1
        elif defect == "moved":
            value["cgroup"] = "0::/other"
        else:
            value["nspids"].append(1)
        return value

    monkeypatch.setattr(item.view, "read_observed", read)
    try:
        with pytest.raises(SandboxUnavailable):
            sampled.check(final=True)
    finally:
        sampled.close()


@pytest.mark.parametrize("defect", ["foreign", "missing", "no-wrapper", "denied", "changed"])
def test_initial_sample_requires_full_original_identity_and_handle(
    live_sample, monkeypatch, defect
):
    item = live_sample
    if defect == "foreign":
        item.group = "0::/other"
    elif defect == "missing":
        reap(item)
    elif defect == "no-wrapper":
        monkeypatch.delattr(os, "pidfd_open")
    elif defect == "denied":

        def denied(_pid):
            raise OSError(errno.EPERM, "denied")

        monkeypatch.setattr(os, "pidfd_open", denied)
    else:
        original = item.view.read_observed
        calls = 0

        def changed(pid):
            nonlocal calls
            value = original(pid)
            calls += 1
            value["birth"] += calls - 1
            return value

        monkeypatch.setattr(item.view, "read_observed", changed)
    with pytest.raises(SandboxUnavailable):
        SampledProcess(item.view, item.process.pid, item.group)


@pytest.mark.parametrize("ending", ["success", "query-error", "wrong-union", "root-changed"])
def test_sample_window_closes_real_handles_on_success_and_each_failure(live_sample, ending):
    item = live_sample
    observer = object.__new__(OwnedProcesses)
    observer.control = SimpleNamespace(host_processes=item.view)
    calls = []

    def check():
        calls.append("original-task")
        if len(calls) == 2 and ending == "root-changed":
            raise SandboxUnavailable("Original task changed")
        return {"cgroup": item.group}

    observer.check = check

    def query(capture):
        capture([item.process.pid])
        reap(item)
        capture([])
        if ending == "query-error":
            raise SandboxUnavailable("Source query failed")
        return [] if ending == "wrong-union" else [item.process.pid]

    if ending == "success":
        assert observer.observe_samples(query) == [item.process.pid]
        assert len(calls) == 2
        # A later observation cannot reuse the completed query's binding.
        with pytest.raises(ProcessDisappeared):
            observer.observe_samples(lambda capture: capture([item.process.pid]))
    else:
        with pytest.raises(SandboxUnavailable):
            observer.observe_samples(query)


def test_handle_does_not_accept_a_different_kernel_descriptor(live_sample, monkeypatch):
    item = live_sample
    actual = os.pidfd_open
    monkeypatch.setattr(os, "pidfd_open", lambda _pid: actual(os.getpid()))
    with pytest.raises(SandboxUnavailable, match="changed its original PID"):
        SampledProcess(item.view, item.process.pid, item.group)


def test_termination_window_uses_real_handles_and_waits_for_runtime_confirmation(live_sample):
    item = live_sample
    observer = object.__new__(OwnedProcesses)
    observer.control = SimpleNamespace(host_processes=item.view)
    deadline = time.monotonic() + 8
    observer.record = {"purpose": "maintenance", "arguments": ["unused", str(deadline - 5)]}
    original = item.view.read(item.process.pid)
    confirmed_at = None
    calls = []

    def runtime():
        # Explicit Docker reporting model around an actual reaped CPU process.
        if confirmed_at is not None:
            if time.monotonic() >= confirmed_at:
                return None
            raise ProcessDisappeared("Original runtime has not yet confirmed exit")
        assert item.view.read(item.process.pid) == original
        return original

    def query(capture):
        nonlocal confirmed_at
        calls.append(time.monotonic())
        if len(calls) == 1:
            capture([item.process.pid])
            reap(item)
            confirmed_at = time.monotonic() + 0.05
            return [item.process.pid]
        assert time.monotonic() >= confirmed_at
        capture([])
        return []

    observer.check_empty_termination = runtime
    assert observer.observe_samples(query, termination_deadline=deadline) == [item.process.pid]
    assert len(calls) == 2 and calls[1] >= confirmed_at


@pytest.mark.parametrize(
    "chain,accepted",
    [
        ("1234 44 1", True),
        ("1234", False),
        ("1234 45 1", False),
        ("1234 44 2", False),
        ("4321 44 1", False),
        ("1234 44 1\nNSpid: 1234 44 1", False),
    ],
)
def test_handle_requires_the_complete_original_target_namespace_chain(monkeypatch, chain, accepted):
    sampled = object.__new__(SampledProcess)
    sampled.fd, sampled.pid = 12, 1234
    sampled.identity = {"nspids": [1234, 44, 1]}

    def read(path, *_args, **_kwargs):
        assert path == Path("/proc/self/fdinfo/12")
        return io.StringIO("Pid: 1234\nNSpid: " + chain + "\n")

    monkeypatch.setattr(Path, "open", read)
    if accepted:
        sampled.bound()
    else:
        with pytest.raises(SandboxUnavailable):
            sampled.bound()
