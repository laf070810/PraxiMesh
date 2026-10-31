"""Trusted flow ordering; modeled sessions do not prove CUDA or device recovery."""

import copy
import time
from types import SimpleNamespace

import pytest

from judge import container_kernel_flow as module
from judge.gpu_task_protocol import task_request
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_adapter import problem, result


@pytest.fixture
def flow(monkeypatch):
    events, sent = [], []
    request = task_request(problem(), "opaque fixture data", "run")
    incoming = [request, {"event": "finish"}]

    class Channel:
        def send(self, value):
            events.append("send:" + value["event"])
            sent.append(copy.deepcopy(value))

        def receive(self):
            return incoming.pop(0)

        def connected(self):
            events.append("connected")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_error):
            events.append("task-stopped")
            runtime.kernel_cleanup.append({"id": "task", "stopped": True, "removed": True})

    class Runtime:
        deadline = time.monotonic() + 100

        def __init__(self):
            self.kernel_cleanup = []

        def tick(self):
            events.append("tick")

        def kernel_session(self, label, **kwargs):
            assert label == "task" and kwargs["mode"] == "run"
            kwargs["on_tick"]()
            events.append("task-started")
            return Session()

        def close(self):
            events.append("runtime-closed")

    runtime = Runtime()

    def evaluate(p, source, mode, _session):
        assert p == problem() and source == request["source"] and mode == "run"
        events.append("compared")
        return result()

    def recover(_runtime):
        events.append("recovered")
        runtime.kernel_cleanup.append({"id": "device-recovery", "stopped": True, "removed": True})

    monkeypatch.setattr(module, "evaluate_kernel", evaluate)
    monkeypatch.setattr(module, "recover_device", recover)

    def run():
        module.serve_kernel(Channel(), runtime, {"event": "fixture-ready"})

    return SimpleNamespace(runtime=runtime, events=events, sent=sent, incoming=incoming, run=run)


def test_result_waits_for_task_stop_independent_recovery_and_runtime_close(flow):
    flow.run()
    events = flow.events
    assert events.index("task-stopped") < events.index("recovered") < events.index("runtime-closed")
    assert events.index("runtime-closed") < events.index("send:kernel-completed")
    assert flow.sent[-1]["ready"] is False


@pytest.mark.parametrize("failure", ["evaluate_kernel", "recover_device"])
def test_platform_or_recovery_failure_withholds_result_and_closes_runtime(
    flow, monkeypatch, failure
):
    def fail(*_args):
        raise SandboxUnavailable("fixed failure")

    monkeypatch.setattr(module, failure, fail)
    with pytest.raises(SandboxUnavailable, match="fixed"):
        flow.run()
    assert not any(item["event"] == "kernel-completed" for item in flow.sent)
    assert flow.events[-1] == "runtime-closed"


def test_close_failure_withholds_result(flow, monkeypatch):
    def fail():
        raise SandboxUnavailable("retained runtime")

    monkeypatch.setattr(flow.runtime, "close", fail)
    with pytest.raises(SandboxUnavailable, match="retained"):
        flow.run()
    assert not any(item["event"] == "kernel-completed" for item in flow.sent)


def test_recovery_cannot_omit_its_guest_cleanup(flow, monkeypatch):
    monkeypatch.setattr(module, "recover_device", lambda _runtime: None)
    with pytest.raises(SandboxUnavailable, match="cleanup"):
        flow.run()
    assert not any(item["event"] == "kernel-completed" for item in flow.sent)


def test_maintenance_never_creates_task_or_returns_task_result(flow):
    flow.incoming[0] = {"event": "recover"}
    flow.run()
    assert "task-started" not in flow.events and "compared" not in flow.events
    assert "result" not in flow.sent[-1]


def test_exhausted_budget_preserves_recovery_margin_and_starts_no_guest(flow):
    flow.runtime.deadline = time.monotonic() + 40
    with pytest.raises(SandboxUnavailable, match="budget"):
        flow.run()
    assert "task-started" not in flow.events and flow.events[-1] == "runtime-closed"


def test_fixed_check_finishes_before_new_recovery_and_never_returns_submission(flow, monkeypatch):
    from tests.test_container_kernel_checks import REQUEST, check_report

    deadline = flow.runtime.deadline
    flow.incoming[0] = copy.deepcopy(REQUEST)

    def check(runtime, request):
        assert request == REQUEST and runtime.deadline == deadline - 45
        flow.events.append("check")
        runtime.kernel_cleanup.append({"id": "startup-check", "stopped": True, "removed": True})
        return check_report()

    monkeypatch.setattr(module, "check_kernel", check)
    flow.run()
    assert flow.events.index("check") < flow.events.index("recovered")
    assert flow.events.index("runtime-closed") < flow.events.index("send:kernel-completed")
    assert "result" not in flow.sent[-1] and flow.sent[-1]["report"] == check_report()
    assert "task-started" not in flow.events and flow.runtime.deadline == deadline


def test_failed_named_check_withholds_report_and_closes_original_runtime(flow, monkeypatch):
    from tests.test_container_kernel_checks import REQUEST

    flow.incoming[0] = copy.deepcopy(REQUEST)

    def fail(*_args):
        raise SandboxUnavailable("fixed startup failure")

    monkeypatch.setattr(module, "check_kernel", fail)
    with pytest.raises(SandboxUnavailable, match="startup failure"):
        flow.run()
    assert "recovered" not in flow.events and flow.events[-1] == "runtime-closed"
    assert not any(item["event"] == "kernel-completed" for item in flow.sent)
