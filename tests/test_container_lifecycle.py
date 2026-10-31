"""Modeled fault boundaries; runtime evidence uses actual CPU acceptance."""

from types import SimpleNamespace

import pytest

from judge import container_lifecycle as lifecycle
from judge.container_kernel_protocol import checked_completion, validate_request
from judge.controller_faults import CHECKS, fault_request, phase
from judge.sandbox import SandboxUnavailable

DEVICE = "GPU-12345678-1234-5678-9abc-123456789abc"
REQUEST = fault_request("controller-exit", "a" * 32)


def cleanup(container="c" * 64, nonce="b" * 32, device=DEVICE):
    return {
        "container": container,
        "removed": True,
        "ready": False,
        "history": f"{device}.{nonce}.done.json",
    }


def report(request=REQUEST, *, device=DEVICE):
    transport = request["name"] == "transport-loss"
    return {
        "scope": lifecycle.SCOPE,
        "name": request["name"],
        "challenge": request["challenge"],
        "container_id": "c" * 64,
        "nonce": "b" * 32,
        "hard_limit_seconds": 50.0,
        "disconnect_seconds": 50.1 if request["name"] == "service-deadline" else 1.0,
        "independent_stop": not transport,
        "armed": lifecycle.armed_message(request),
        "termination": {
            "running": transport,
            "pid": 123 if transport else 0,
            "exit_code": 0 if transport else 124 if request["name"] == "service-deadline" else 70,
            "oom_killed": False,
        },
        "cleanup": cleanup(device=device),
    }


@pytest.fixture
def observer(monkeypatch):
    values = SimpleNamespace(now=100.0, eof=False, name="controller-exit", defect=None)
    events = []
    args = ["105.0", "150.0", "b" * 32, "fixed"]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: values.now)
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda seconds: setattr(values, "now", values.now + seconds)
    )

    class Service:
        guard = SimpleNamespace(assert_held=lambda: events.append("lock"))
        engine = SimpleNamespace(check_socket=lambda: events.append("socket"))

        def inspect(self):
            running = not values.eof or values.name == "transport-loss"
            if (
                values.defect == "stopped-before"
                or values.eof
                and values.defect == "transport-stopped"
            ):
                running = False
            if values.defect == "never-stops":
                running = True
            result = {
                "id": "c" * 64,
                "arguments": list(args),
                "running": running,
                "pid": 123 if running else 0,
                "oom_killed": False,
                "exit_code": 0 if running else 124 if values.name == "service-deadline" else 70,
            }
            if values.eof:
                if values.defect == "missing":
                    return None
                if values.defect == "id":
                    result["id"] = "d" * 64
                if values.defect == "nonce":
                    result["arguments"][2] = "c" * 32
                if values.defect == "deadline":
                    result["arguments"][1] = "151.0"
                if values.defect == "oom":
                    result["oom_killed"] = True
                if values.defect == "exit":
                    result["exit_code"] = 137
            return result

        def stop(self):
            pytest.fail("Observer cannot stop a container to prove independent termination")

    class Channel:
        deadline = 150.0
        on_tick = None
        next_tick = 0

        def send(self, message):
            events.append(message)

        def expect_eof(self):
            if values.defect == "output":
                raise SandboxUnavailable("Unexpected trailing output")
            values.eof = True
            values.now = (
                150.1 if values.name == "service-deadline" and values.defect != "early" else 101.0
            )
            self.on_tick()

    channel = Channel()
    connection = SimpleNamespace(channel=channel, deadline=150.0, identity=("c" * 64, tuple(args)))

    def run():
        request = fault_request(values.name, "a" * 32)
        armed = lifecycle.armed_message(request)
        if values.defect == "challenge":
            armed["challenge"] = "b" * 32
        return lifecycle.observe_fault(
            connection,
            Service(),
            request,
            armed,
            started=100.0,
            deadline=155.0,
            on_tick=lambda: events.append("lease"),
        )

    return values, events, channel, run


@pytest.mark.parametrize("name", CHECKS)
def test_matching_fault_requires_live_arm_and_expected_independent_state(observer, name):
    values, events, channel, run = observer
    values.name = name
    result = run()
    request = fault_request(name, "a" * 32)
    lifecycle.validate_observation(result, request)
    assert result["termination"]["running"] is (name == "transport-loss")
    assert result["independent_stop"] is (name != "transport-loss")
    assert channel.deadline == 155.0 and events.count(phase(request, "fault-trigger")) == 1
    assert "lease" in events and "lock" in events


@pytest.mark.parametrize(
    "defect",
    [
        "stopped-before",
        "missing",
        "id",
        "nonce",
        "deadline",
        "oom",
        "exit",
        "output",
        "early",
        "challenge",
        "never-stops",
    ],
)
def test_early_unmatched_unstopped_or_replaced_faults_are_rejected(observer, defect):
    values, _events, _channel, run = observer
    values.name, values.defect = "service-deadline", defect
    with pytest.raises(SandboxUnavailable):
        run()


def test_transport_fault_must_leave_real_cleanup_work(observer):
    values, _events, _channel, run = observer
    values.name, values.defect = "transport-loss", "transport-stopped"
    with pytest.raises(SandboxUnavailable, match="alive"):
        run()


@pytest.mark.parametrize("name", CHECKS)
def test_fresh_recovery_and_original_archive_are_required(name):
    request = fault_request(name, "a" * 32)
    assert validate_request(request) == request
    value = report(request)
    lifecycle.validate_report(value, request, device_uuid=DEVICE)
    value.update(
        recovered=True,
        recovery={
            "container_id": "d" * 64,
            "nonce": "c" * 32,
            "cleanup": cleanup("d" * 64, "c" * 32),
        },
    )
    lifecycle.validate_report(value, request, device_uuid=DEVICE, recovered=True)
    with pytest.raises(SandboxUnavailable, match="normal kernel"):
        checked_completion({"event": "kernel-completed"}, request)


@pytest.mark.parametrize(
    "path,value",
    [
        (("scope",), "host-controller-check"),
        (("challenge",), "b" * 32),
        (("hard_limit_seconds",), 91),
        (("hard_limit_seconds",), True),
        (("disconnect_seconds",), float("nan")),
        (("disconnect_seconds",), -1),
        (("disconnect_seconds",), 60),
        (("independent_stop",), 1),
        (("armed", "guest", "correct"), 1),
        (("armed", "guest", "attach_alive"), False),
        (("termination", "running"), 0),
        (("termination", "exit_code"), 124),
        (("termination", "pid"), True),
        (("termination", "oom_killed"), True),
        (("cleanup", "container"), "d" * 64),
        (("cleanup", "removed"), 1),
        (("cleanup", "ready"), True),
        (("cleanup", "history"), "other.done.json"),
    ],
)
def test_fault_claims_without_exact_identity_measurement_and_cleanup_are_rejected(path, value):
    result = report()
    item = result
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with pytest.raises(SandboxUnavailable):
        lifecycle.validate_report(result, REQUEST, device_uuid=DEVICE)


@pytest.mark.parametrize("defect", ["container", "nonce", "cleanup", "recovered", "missing"])
def test_reused_or_incomplete_recovery_cannot_finish_a_check(defect):
    result = report()
    result.update(
        recovered=True,
        recovery={
            "container_id": "d" * 64,
            "nonce": "c" * 32,
            "cleanup": cleanup("d" * 64, "c" * 32),
        },
    )
    if defect == "container":
        result["recovery"]["container_id"] = result["container_id"]
    elif defect == "nonce":
        result["recovery"]["nonce"] = result["nonce"]
    elif defect == "cleanup":
        result["recovery"]["cleanup"] = result["cleanup"]
    elif defect == "recovered":
        result["recovered"] = 1
    else:
        result["recovery"].pop("cleanup")
    with pytest.raises(SandboxUnavailable):
        lifecycle.validate_report(result, REQUEST, device_uuid=DEVICE, recovered=True)
