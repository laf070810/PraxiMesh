"""Fault handshake and independent-stop boundaries; no GPU execution."""

from types import SimpleNamespace

import pytest

from judge import controller_faults
from judge import gpu_lifecycle as lifecycle
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def observer(monkeypatch):
    state = SimpleNamespace(
        now=100.0,
        closed=False,
        name="controller-exit",
        early=False,
        never_stops=False,
        before_trigger=False,
        wrong_challenge=False,
        extra_output=False,
        disappears_after_eof=False,
    )
    events = []
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds)
    )

    class Service:
        runtime_limit = 10.0

        def inspect(self):
            if state.before_trigger or (
                state.closed
                and (state.name != "transport-loss" or state.disappears_after_eof)
                and not state.never_stops
            ):
                return None
            return {"ActiveState": "active", "MainPID": "123"}

        def populated(self):
            return self.inspect() is not None

        def stop(self):
            pytest.fail("Observer must not force stop to prove independent termination")

    class Channel:
        deadline = 110.0

        def receive(self):
            request = lifecycle.fault_request(state.name, "a" * 32)
            value = lifecycle.phase(request, "fault-armed")
            if state.wrong_challenge:
                value["challenge"] = "b" * 32
            return value

        def send(self, value):
            events.append(value)

        def expect_eof(self):
            if state.extra_output:
                raise SandboxUnavailable("Unexpected trailing controller output")
            state.closed = True
            state.now += 10.0 if state.name == "service-deadline" and not state.early else 1.0

    channel = Channel()

    def run():
        return lifecycle.observe_fault(
            channel,
            Service(),
            lifecycle.fault_request(state.name, "a" * 32),
            started=100.0,
            deadline=120.0,
            on_tick=lambda: events.append("tick"),
        )

    return state, events, channel, run


@pytest.mark.parametrize("name", lifecycle.CHECKS)
def test_only_armed_matching_faults_observe_expected_service_state(observer, name):
    state, events, channel, run = observer
    state.name = name
    report = run()
    assert report["name"] == name and report["challenge"] == "a" * 32
    assert report["independent_stop"] is (name != "transport-loss")
    assert report["disconnect_seconds"] == (10.0 if name == "service-deadline" else 1.0)
    assert channel.deadline == 119.0
    assert (
        events.count(lifecycle.phase(lifecycle.fault_request(name, "a" * 32), "fault-trigger")) == 1
    )


@pytest.mark.parametrize(
    "defect", ["early", "before_trigger", "wrong_challenge", "never_stops", "extra_output"]
)
def test_early_or_unmatched_disconnect_is_not_a_success(observer, defect):
    state, _events, _channel, run = observer
    state.name = "service-deadline"
    setattr(state, defect, True)
    with pytest.raises(SandboxUnavailable):
        run()


def test_transport_fault_requires_live_processes_after_disconnect(observer):
    state, _events, _channel, run = observer
    state.name = "transport-loss"
    # Model an EOF followed by the entire service disappearing, rather than a
    # lost channel while the workload still needs the host supervisor.
    state.disappears_after_eof = True
    with pytest.raises(SandboxUnavailable, match="live service"):
        run()


@pytest.mark.parametrize("name", lifecycle.CHECKS)
def test_fault_is_injected_only_after_matching_trigger(monkeypatch, name):
    request = lifecycle.fault_request(name, "a" * 32)
    events = []

    class Injected(BaseException):
        pass

    class Channel:
        def send(self, value):
            events.append(value)

        def receive(self):
            return lifecycle.phase(request, "fault-trigger")

        def close(self):
            events.append("closed")

    def exit_process(code):
        events.append(("exit", code))
        raise Injected

    def sleep(_seconds):
        events.append("no-polling")
        raise Injected

    monkeypatch.setattr(controller_faults.os, "_exit", exit_process)
    monkeypatch.setattr(lifecycle.time, "sleep", sleep)
    with pytest.raises(Injected):
        lifecycle.trigger_fault(Channel(), request)
    assert events[0] == lifecycle.phase(request, "fault-armed")
    assert ("closed" in events) is (name == "transport-loss")
    assert (("exit", 37) in events) is (name == "controller-exit")


@pytest.mark.parametrize("extra", [False, True])
def test_fault_trigger_cannot_replay_an_old_challenge_or_supply_commands(extra):
    request = lifecycle.fault_request("controller-exit", "a" * 32)
    received = lifecycle.phase(request, "fault-trigger")
    if extra:
        received["command"] = "not allowed"
    else:
        received["challenge"] = "b" * 32
    channel = SimpleNamespace(send=lambda value: None, receive=lambda: received)
    with pytest.raises(SandboxUnavailable, match="trigger"):
        lifecycle.trigger_fault(channel, request)


@pytest.mark.parametrize(
    "message",
    [
        {},
        None,
        {
            "event": "check-controller",
            "name": "controller-exit",
            "challenge": "a" * 32,
            "source": "not allowed",
        },
    ],
)
def test_untrusted_requests_cannot_arm_a_fault(message):
    with pytest.raises(SandboxUnavailable, match="Unexpected"):
        lifecycle.validate_request(message)
