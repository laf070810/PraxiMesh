"""Fixed entry refusal and real signal handling; no GPU methods are called."""

import os
import select
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import container_practice_entry as entry
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_practice import local as local
from tests.test_container_practice_protocol import Frames


def args(mode="initialize"):
    return [mode, "GPU-" + str(uuid.uuid4()), uuid.uuid4().hex, str(time.monotonic() + 10)]


@pytest.mark.parametrize(
    "values", [[], ["run"], args("submit"), [*args()[:3], "nan"], [*args()[:3], "inf"]]
)
def test_invalid_entry_arguments_refuse_before_controller_or_pipe(values, monkeypatch, capsys):
    monkeypatch.setattr(
        entry, "ContainerController", lambda *args: pytest.fail("Controller constructed")
    )
    monkeypatch.setattr(entry, "PipeChannel", lambda *args, **kwargs: pytest.fail("Pipe opened"))
    assert entry.main(values) == 2
    captured = capsys.readouterr()
    assert (
        captured.out == ""
        and captured.err == "Local controller unavailable; retain original state for recovery.\n"
    )


def test_fixed_initialization_preserves_binding_and_never_builds_a_task(local, monkeypatch):
    @contextmanager
    def control(device):
        assert device == local.guard.device_uuid
        yield local.control

    channels = []

    @contextmanager
    def pipe(incoming, outgoing, *, deadline, on_tick):
        assert (incoming, outgoing) == (0, 1)
        value = Frames([{"event": "initialize"}], deadline=deadline)
        value.on_tick = on_tick
        channels.append(value)
        yield value

    monkeypatch.setattr(entry, "ContainerController", control)
    monkeypatch.setattr(entry, "PipeChannel", pipe)
    values = args()
    values[1] = local.guard.device_uuid
    original = dict(local.journal.identity)
    assert entry.main(values) == 0
    marker = local.journal.read("local-practice.json")
    assert marker["binding"]["installation"] == original
    assert local.journal.identity == original and local.journal.load() is None
    assert local.events == [] and local.public_calls == 0
    assert channels[0].sent[-1]["result"] == {
        "status": "initialized",
        "ready": False,
        "formal_result": False,
    }


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_actual_signal_is_deferred_to_normal_control_path(number):
    root = Path(__file__).resolve().parents[1]
    program = f"""
import signal,sys,time
sys.path.insert(0,{str(root)!r})
from scripts.container_practice_entry import cancellation
original=signal.getsignal(signal.SIGTERM)
try:
    with cancellation() as check:
        print('ready',flush=True)
        while True:
            check()
            time.sleep(0.01)
except InterruptedError:
    assert signal.getsignal(signal.SIGTERM)==original
    print('cancelled-through-check',flush=True)
    raise SystemExit(130)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline() == b"ready\n"
        os.kill(process.pid, number)
        stdout, stderr = process.communicate(timeout=5)
        assert (
            process.returncode == 130 and stdout == b"cancelled-through-check\n" and stderr == b""
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()
