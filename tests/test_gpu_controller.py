"""Trusted inner state-machine checks; these do not execute CUDA or certify recovery."""

import time
from types import SimpleNamespace

import pytest

from judge import gpu_controller
from judge.gpu_execution_contract import task_request
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_adapter import TARGET, problem, result


@pytest.fixture
def controller(monkeypatch, tmp_path):
    events = []
    incoming = [
        {"event": "prepare", "target": TARGET},
        task_request(problem(), "fixed unit-test source", "run"),
        {"event": "finish"},
    ]

    class Channel:
        def send(self, message):
            events.append(("send", message))

        def receive(self):
            return incoming.pop(0)

        def connected(self):
            events.append(("connected", None))

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append(("guest-stopped", None))

    class Runtime:
        def __init__(self, _base, **_kwargs):
            pass

        def prepare(self, *_args):
            events.append(("prepare", None))

        def prepare_gpu(self, *_args):
            return {"scope": "unit tests only"}

        def tick(self):
            events.append(("tick", None))

        def kernel_session(self, label, **_kwargs):
            events.append(("session", label))
            return Session()

        def close(self):
            events.append(("store-closed", None))

    def evaluate(_problem, source, mode, session):
        session.on_tick()
        events.append(("evaluated", (source, mode)))
        return result()

    monkeypatch.setattr(gpu_controller, "PrivateRuntime", Runtime)
    monkeypatch.setattr(gpu_controller, "evaluate_kernel", evaluate)
    monkeypatch.setattr(
        gpu_controller, "recover_device", lambda _runtime: events.append(("recovery-checked", None))
    )
    return SimpleNamespace(
        events=events,
        incoming=incoming,
        run=lambda deadline=None: gpu_controller.serve(
            Channel(), tmp_path, (None, None, None), deadline or time.monotonic() + 100
        ),
    )


def test_result_follows_guest_recovery_and_store_cleanup(controller):
    controller.run()
    events = controller.events
    completed = next(
        i
        for i, (name, message) in enumerate(events)
        if name == "send" and message["event"] == "completed"
    )
    assert (
        events.index(("guest-stopped", None))
        < events.index(("recovery-checked", None))
        < events.index(("store-closed", None))
        < completed
    )


def test_failed_recovery_has_no_completion_frame(controller, monkeypatch):
    def failed(_runtime):
        raise SandboxUnavailable("fixed recovery failure")

    monkeypatch.setattr(gpu_controller, "recover_device", failed)
    with pytest.raises(SandboxUnavailable, match="recovery failure"):
        controller.run()
    assert ("store-closed", None) in controller.events
    assert not any(
        name == "send" and message["event"] == "completed" for name, message in controller.events
    )


def test_expired_task_budget_keeps_recovery_margin_and_runs_no_guest(controller):
    with pytest.raises(SandboxUnavailable, match="Insufficient"):
        controller.run(time.monotonic() + 40)
    assert not any(name == "session" for name, _message in controller.events)
    assert ("store-closed", None) in controller.events


def test_task_cannot_be_sent_before_preparation(controller):
    controller.incoming[0] = controller.incoming[1]
    with pytest.raises(SandboxUnavailable, match="preparation"):
        controller.run()
    assert ("prepare", None) not in controller.events


def test_extra_execution_fields_are_rejected(controller):
    controller.incoming[1]["untrusted_command"] = "anything"
    with pytest.raises(SandboxUnavailable, match="execution request"):
        controller.run()
    assert not any(name == "session" for name, _message in controller.events)


def test_independent_recovery_does_not_accept_a_wrong_output():
    events = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_error):
            events.append("stopped")

        def compile(self, source, operation):
            assert source == gpu_controller.VECTOR_REFERENCE
            assert operation == "vector-add-f32-v1"

        def execute(self, case):
            assert "expected" not in case
            return [999.0] * 257, 1.0

        def finish(self):
            pytest.fail("Wrong recovery cannot finish successfully")

    class Runtime:
        def kernel_session(self, name, *, mode):
            assert name == "device-recovery" and mode == "run"
            return Session()

    with pytest.raises(SandboxUnavailable, match="correctness recovery failed"):
        gpu_controller.recover_device(Runtime())
    assert events == ["stopped"]


def test_maintenance_request_runs_no_learner_session_and_returns_no_score(controller):
    controller.incoming[1] = {"event": "recover"}
    controller.run()
    assert not any(name in {"session", "evaluated"} for name, _message in controller.events)
    assert ("recovery-checked", None) in controller.events
    message = next(
        message
        for name, message in controller.events
        if name == "send" and message["event"] == "recovered"
    )
    assert message == {"event": "recovered", "recovered": True}


def test_recovery_request_with_source_is_rejected(controller):
    controller.incoming[1] = {"event": "recover", "source": "not accepted"}
    with pytest.raises(SandboxUnavailable, match="execution request"):
        controller.run()
    assert ("recovery-checked", None) not in controller.events


def test_resource_checks_still_require_independent_gpu_recovery(controller, monkeypatch):
    from tests.test_gpu_isolation import REQUEST, resource_report

    def resources(runtime, request, *, expect_gpu, record):
        assert expect_gpu is True and request == REQUEST
        assert runtime.deadline < time.monotonic() + 60
        controller.events.append(("resources-checked", None))
        return resource_report(request, TARGET)

    monkeypatch.setattr(gpu_controller, "check_resources", resources)
    controller.incoming[1] = {"event": "check-resources", **REQUEST}
    controller.run()
    events = controller.events
    assert not any(name in {"session", "evaluated"} for name, _value in events)
    completed = next(
        i
        for i, (name, message) in enumerate(events)
        if name == "send" and message["event"] == "resources-checked"
    )
    assert (
        events.index(("resources-checked", None))
        < events.index(("recovery-checked", None))
        < events.index(("store-closed", None))
        < completed
    )


def test_resource_request_cannot_include_source(controller):
    controller.incoming[1] = {
        "event": "check-resources",
        "canary": "a" * 32,
        "port": 12345,
        "source": "not accepted",
    }
    with pytest.raises(SandboxUnavailable, match="resource check request"):
        controller.run()
    assert ("recovery-checked", None) not in controller.events


def test_failed_resource_check_cannot_return_a_ready_report(controller, monkeypatch):
    from tests.test_gpu_isolation import REQUEST

    def failed(*_args, **_kwargs):
        raise SandboxUnavailable("fixed resource check failure")

    monkeypatch.setattr(gpu_controller, "check_resources", failed)
    controller.incoming[1] = {"event": "check-resources", **REQUEST}
    with pytest.raises(SandboxUnavailable, match="resource check failure"):
        controller.run()
    assert ("store-closed", None) in controller.events
    assert not any(
        name == "send" and message["event"] == "resources-checked"
        for name, message in controller.events
    )
