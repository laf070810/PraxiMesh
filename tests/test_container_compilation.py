"""Protocol/lifecycle boundaries; real isolation uses controller acceptance."""

import copy
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from judge import container_compilation as module
from judge.container_compile_protocol import request
from judge.container_compiler import compiler_digest
from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_controller import CONTROLLER_SOURCES, ROOT, controller_digest
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def flow(monkeypatch):
    events = []
    compiled = {
        "event": "compiled",
        "architecture": [8, 6],
        "ptx_bytes": 32,
        "ptx_sha256": "a" * 64,
    }
    completion = {
        "event": "compile-checked",
        "status": "compiled",
        "ready": False,
        "executed": False,
        "cleanup": copy.deepcopy(module.GUEST_CLEANUP),
        "compilation": copy.deepcopy(compiled),
    }
    incoming = [
        {
            "event": "compiler-ready",
            "nvrtc_version": [13, 1],
            "gpu_nodes": [],
            "cuda_driver_available": False,
        },
        {"event": "compiled", "compilation": compiled},
        completion,
    ]

    class Journal:
        record = None

        def check(self):
            events.append("journal-check")

        def load(self):
            return self.record

        def recover(self):
            events.append("recover")
            self.record = None
            return {"container": "b" * 64, "removed": True, "ready": False}

    class Service:
        def __init__(self, journal, _job, _reservation, *, image, resource_profile):
            self.journal, self.image, self.profile = journal, image, resource_profile
            self.container_id = None
            self.owner = "original-owner"

        def start(self, command, **_kwargs):
            events.append("start")
            self.container_id = "b" * 64
            self.journal.record = {
                "owner": self.owner,
                "image": self.image,
                "version": 2,
                "resource_profile": self.profile,
                "container_id": self.container_id,
            }
            return {"limits": resource_limits(self.profile)}

    class Connection:
        def __init__(self, _service, _status, *, on_tick):
            events.append("connect")
            self.channel, self.on_tick = self, on_tick

        def tick(self):
            self.on_tick()

        def send(self, value):
            events.append(("send", copy.deepcopy(value)))

        def receive(self):
            return incoming.pop(0)

        def finish(self):
            events.append("finish")

        def close(self):
            events.append("close")

    monkeypatch.setattr(module, "ContainerJournal", Journal)
    monkeypatch.setattr(module, "CompilerContainerService", Service)
    monkeypatch.setattr(module, "ContainerConnection", Connection)
    journal = Journal()
    task = module.CompilationTask(journal, "job", "reservation", image="sha256:" + "c" * 64)
    message = request('extern "C" __global__ void solve() {}', "vector-add-f32-v1", [8, 6])

    def run(**options):
        return task.run(
            message,
            deadline=100,
            on_begin=options.pop("on_begin", lambda: events.append("begin")),
            on_tick=options.pop("on_tick", lambda: None),
            **options,
        )

    return SimpleNamespace(
        task=task,
        journal=journal,
        events=events,
        incoming=incoming,
        compiled=compiled,
        message=message,
        run=run,
        Connection=Connection,
    )


def test_result_waits_for_begin_original_cleanup_and_cannot_repeat(flow):
    result = flow.run(on_compiled=lambda value: value.update(ptx_sha256="f" * 64))
    sent = [i for i, event in enumerate(flow.events) if type(event) is tuple]
    assert flow.events.index("begin") < min(sent)
    assert flow.events[-3:] == ["finish", "close", "recover"]
    assert flow.journal.record is None and flow.task.cleanup["removed"] is True
    assert result["compilation"]["ptx_sha256"] == "a" * 64
    assert result["artifacts"]["resource_profile"] == WORKLOAD_PROFILE
    assert result["ready"] is result["executed"] is False
    with pytest.raises(ValueError, match="once"):
        flow.run()


def test_invalid_request_never_starts_a_container(flow):
    flow.message["verdict"] = "accepted"
    with pytest.raises(ValueError):
        flow.run()
    assert "start" not in flow.events and "begin" not in flow.events
    assert flow.journal.record is None


def test_failed_begin_sends_no_source_and_recovers(flow):
    def fail():
        raise module.CompilationCancelled("cancelled")

    with pytest.raises(module.CompilationCancelled):
        flow.run(on_begin=fail)
    assert not any(type(event) is tuple for event in flow.events)
    assert flow.events[-2:] == ["close", "recover"]


def test_record_change_during_begin_refuses_source_and_another_tasks_cleanup(flow):
    def change_record():
        flow.journal.record["owner"] = "replacement-owner"

    with pytest.raises(SandboxUnavailable, match="ownership"):
        flow.run(on_begin=change_record)
    assert not any(type(event) is tuple for event in flow.events)
    assert "recover" not in flow.events


def test_pending_journal_must_be_recovered_before_a_new_task(flow):
    flow.journal.record = {"owner": "older-task"}
    with pytest.raises(SandboxUnavailable, match="previous task"):
        module.CompilationTask(flow.journal, "other", "reservation", image="sha256:" + "c" * 64)
    assert "start" not in flow.events and flow.journal.record == {"owner": "older-task"}


def test_cancellation_during_live_tick_keeps_original_cleanup(flow):
    def fail():
        raise module.CompilationCancelled("lease lost")

    with pytest.raises(module.CompilationCancelled):
        flow.run(on_tick=fail)
    assert not any(type(event) is tuple for event in flow.events)
    assert flow.events[-2:] == ["close", "recover"]


def test_compilation_cancel_is_not_a_learner_error(flow):
    def fail(_value):
        raise module.CompilationCancelled("cancelled after compile")

    with pytest.raises(module.CompilationCancelled):
        flow.run(on_compiled=fail)
    assert flow.events[-2:] == ["close", "recover"]
    assert ("send", {"event": "finish"}) not in flow.events


def test_close_failure_still_attempts_original_recovery(flow, monkeypatch):
    def fail(_self):
        raise SandboxUnavailable("CLI did not close")

    monkeypatch.setattr(flow.Connection, "close", fail)
    with pytest.raises(SandboxUnavailable, match="CLI did not close"):
        flow.run()
    assert flow.events[-1] == "recover" and flow.journal.record is None


def test_cleanup_failure_never_returns_compilation_success(flow, monkeypatch):
    def fail():
        raise SandboxUnavailable("original container retained")

    monkeypatch.setattr(flow.journal, "recover", fail)
    with pytest.raises(SandboxUnavailable, match="retained"):
        flow.run()
    assert flow.journal.record is not None


@pytest.mark.parametrize("change", ["owner", "image", "profile", "missing"])
def test_changed_journal_is_retained_without_recovering_another_task(flow, change):
    def change_record(_value):
        if change == "missing":
            flow.journal.record = None
        else:
            key = "resource_profile" if change == "profile" else change
            flow.journal.record[key] = "different-task"

    with pytest.raises(SandboxUnavailable, match="record|ownership"):
        flow.run(on_compiled=change_record)
    assert "recover" not in flow.events


@pytest.mark.parametrize(
    "change",
    ["extra", "verdict", "ready", "executed", "cleanup", "boolean", "metadata", "architecture"],
)
def test_unverified_reply_cannot_return_a_result(flow, change):
    reply = flow.incoming[-1]
    if change == "extra":
        reply["source"] = "PRIVATE_SENTINEL"
    elif change == "verdict":
        reply["status"] = "accepted"
    elif change in {"ready", "executed"}:
        reply[change] = True
    elif change == "cleanup":
        reply["cleanup"] = []
    elif change == "boolean":
        reply["cleanup"][0]["removed"] = 1
    elif change == "metadata":
        reply["compilation"]["ptx_sha256"] = "f" * 64
    else:
        reply["compilation"]["architecture"] = [True, 6]
    with pytest.raises(SandboxUnavailable):
        flow.run()
    assert flow.events[-2:] == ["close", "recover"]


@pytest.mark.parametrize("status", sorted(module.ERRORS | {"platform_error"}))
def test_bounded_compiler_errors_keep_cleanup_without_raw_diagnostics(flow, status):
    reply = flow.incoming[-1]
    reply["status"] = status
    del reply["compilation"]
    if status == "platform_error":
        reply["failure_kind"] = "GPUDriverExited"
    del flow.incoming[1]
    result = flow.run()
    assert result["status"] == status and "failure_kind" not in result
    assert "compilation" not in result and flow.journal.record is None


def test_control_snapshot_computes_compiler_identity_without_host_source(tmp_path):
    for name in CONTROLLER_SOURCES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from judge.container_compiler import compiler_digest; "
            "from judge.container_controller import controller_digest; "
            "from judge.container_compilation import CompilationTask; "
            "print(compiler_digest()); print(controller_digest())",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath, "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [compiler_digest(), controller_digest()]
