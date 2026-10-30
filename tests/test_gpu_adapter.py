"""Supervision ordering and result rejection; mocks do not certify GPU recovery."""

import copy
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from judge import gpu_adapter
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from judge.gpu_execution_contract import (
    canonical_digest,
    environment_identity,
    validate_result,
    validate_target,
)
from judge.gpu_policy import PROFILE, profile_digest
from judge.gpu_service import OwnedJobService
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

TARGET = {
    "index": 0,
    "uuid": "GPU-11111111-2222-3333-4444-555555555555",
    "model": "synthetic test device",
    "driver": "590.48.01",
    "architecture": [8, 6],
}


def problem():
    return {
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "operation": "vector-add-f32-v1",
        "entrypoint": "solve",
        "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        "tests": [
            {"name": "public", "public": True, "inputs": {"a": [1], "b": [2]}, "expected": [3]},
            {
                "name": "hidden-name",
                "public": False,
                "inputs": {"a": [2], "b": [4]},
                "expected": [6],
            },
        ],
    }


def result():
    return {
        "verdict": "wrong_answer",
        "label": LABELS["wrong_answer"],
        "passed": 0,
        "total": 1,
        "public_cases": [
            {"name": "public", "verdict": "wrong_answer", "label": LABELS["wrong_answer"]}
        ],
    }


@pytest.fixture
def controlled(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="px-g9-", dir="/tmp") as directory:
        with DeviceLock(Path(directory) / "locks", TARGET["uuid"]) as guard:
            adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
            adapter.target = copy.deepcopy(TARGET)
            adapter.directory = Path(directory) / "jobs"
            adapter.archives = (Path("/fixed/runsc"), Path("/fixed/image"), Path("/fixed/nvrtc"))
            adapter.identity = {"scope": "protocol unit test only"}
            adapter.environment = environment_identity(TARGET, adapter.identity)
            adapter.fingerprint = canonical_digest(adapter.environment)
            events = []
            messages = [
                {
                    "event": "connected",
                    "protocol": 1,
                    "execution_digest": adapter.environment["execution_digest"],
                },
                {
                    "event": "prepared",
                    "identity": copy.deepcopy(adapter.identity),
                    "target": copy.deepcopy(TARGET),
                },
                {"event": "completed", "result": result(), "recovered": True},
            ]

            class Channel:
                def receive(self):
                    message = messages.pop(0)
                    if callable(message):
                        message = message()
                    events.append(("receive", message["event"]))
                    return message

                def send(self, value):
                    events.append(("send", copy.deepcopy(value)))

                def expect_eof(self):
                    events.append(("eof", None))

                def close(self):
                    events.append(("channel-close", None))

            class Listener:
                path = Path("/fixed/private/control.sock")

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    pass

                def accept(self, service, **_kwargs):
                    events.append(("accept", service.unit))
                    return Channel()

            monkeypatch.setattr(gpu_adapter, "PrivateListener", Listener)
            monkeypatch.setattr(gpu_adapter, "identify", lambda _: copy.deepcopy(TARGET))
            monkeypatch.setattr(
                gpu_adapter.DeviceMonitor,
                "observe",
                lambda _self, service=None: events.append(("observe", service is not None)),
            )
            monkeypatch.setattr(OwnedJobService, "inspect", lambda _self: None)
            monkeypatch.setattr(OwnedJobService, "populated", lambda _self: False)
            monkeypatch.setattr(
                OwnedJobService, "stop", lambda _self: events.append(("stop", None))
            )
            monkeypatch.setattr(
                OwnedJobService,
                "start",
                lambda _self, command, **_kwargs: events.append(("start", command)),
            )

            def run(
                *,
                on_begin=None,
                maintenance=False,
                purpose=None,
                resource_check=False,
                kernel=None,
                controller_fault=None,
                controller_seconds=180,
            ):
                service = OwnedJobService(
                    guard,
                    uuid.uuid4(),
                    uuid.uuid4(),
                    purpose=purpose
                    or (
                        "maintenance"
                        if maintenance or resource_check or kernel or controller_fault
                        else "task"
                    ),
                )
                if controller_fault is not None:
                    return adapter.check_controller(
                        service,
                        controller_fault,
                        deadline=time.monotonic() + controller_seconds,
                        on_tick=lambda: events.append(("poll", None)),
                    )
                if kernel is not None:
                    return adapter.check_kernel(
                        service,
                        kernel,
                        deadline=time.monotonic() + 180,
                        on_tick=lambda: events.append(("poll", None)),
                    )
                if resource_check:
                    return adapter.check_resources(
                        service,
                        deadline=time.monotonic() + 180,
                        on_tick=lambda: events.append(("poll", None)),
                    )
                if maintenance:
                    return adapter.recover(
                        service,
                        deadline=time.monotonic() + 90,
                        on_tick=lambda: events.append(("poll", None)),
                    )
                return adapter.evaluate(
                    service,
                    deadline=time.monotonic() + 90,
                    problem=problem(),
                    source="synthetic-source-never-executed",
                    mode="run",
                    on_tick=lambda: events.append(("poll", None)),
                    on_begin=on_begin or (lambda: events.append(("begin", None))),
                )

            yield adapter, messages, events, run


def test_kernel_check_binds_fresh_request_then_waits_for_service_stop(controlled):
    from tests.test_gpu_kernel_checks import check_report

    adapter, messages, events, run = controlled

    def response():
        request = next(
            value for name, value in events if name == "send" and value["event"] == "check-kernel"
        )
        assert set(request) == {"event", "name", "challenge"}
        return {"event": "kernel-checked", "report": check_report(request), "recovered": True}

    messages[-1] = response
    report, environment = run(kernel="public-run")
    assert report["name"] == "public-run" and environment == adapter.environment
    assert not any(name == "begin" for name, _value in events)
    assert events.index(("eof", None)) < events.index(("stop", None))
    assert events[-1] == ("observe", False)
    assert list(adapter.directory.iterdir()) == []


@pytest.mark.parametrize("defect", ["event", "recovery", "challenge", "extra", "verdict"])
def test_kernel_check_rejects_incomplete_or_mismatched_response(controlled, defect):
    from tests.test_gpu_kernel_checks import check_report

    _adapter, messages, events, run = controlled

    def response():
        request = next(
            value for name, value in events if name == "send" and value["event"] == "check-kernel"
        )
        value = {"event": "kernel-checked", "report": check_report(request), "recovered": True}
        if defect == "event":
            value["event"] = "completed"
        elif defect == "recovery":
            value["recovered"] = 1
        elif defect == "challenge":
            value["report"]["challenge"] = "0" * 32
        elif defect == "extra":
            value["report"]["ready"] = True
        else:
            value["report"]["result"].update(
                verdict="wrong_answer", label=LABELS["wrong_answer"], passed=0
            )
        return value

    messages[-1] = response
    with pytest.raises(SandboxUnavailable):
        run(kernel="public-run")
    assert ("stop", None) in events
    assert ("send", {"event": "finish"}) not in events


@pytest.mark.parametrize("purpose", ["task", "recovery"])
def test_kernel_checks_need_a_maintenance_service(controlled, purpose):
    _adapter, _messages, events, run = controlled
    with pytest.raises(ValueError, match="maintenance"):
        run(kernel="public-run", purpose=purpose)
    assert events == []


def test_unknown_kernel_check_is_rejected_before_device_query(controlled):
    _adapter, _messages, events, run = controlled
    with pytest.raises(ValueError, match="fixed CUDA"):
        run(kernel="arbitrary-command")
    assert events == []


def test_controller_recovery_gets_new_invocation_only_after_fault_cleanup(controlled, monkeypatch):
    adapter, _messages, events, run = controlled
    previous = []

    def exchange(service, request, *, deadline, on_tick, on_begin):
        assert request["event"] == "check-controller"
        assert 85 < deadline - time.monotonic() <= 90
        previous.append(service)
        events.append(("fault-cleaned", None))
        return {
            "name": request["name"],
            "challenge": request["challenge"],
            "independent_stop": True,
        }

    def recover(service, *, deadline, on_tick):
        assert events[-1] == ("fault-cleaned", None)
        assert service is not previous[0] and service.unit == previous[0].unit
        assert service.guard is previous[0].guard
        assert service.invocation is None
        assert 85 < deadline - time.monotonic() <= 90
        events.append(("new-recovery", None))
        return copy.deepcopy(adapter.environment)

    monkeypatch.setattr(adapter, "_exchange", exchange)
    monkeypatch.setattr(adapter, "recover", recover)
    report, environment = run(controller_fault="controller-exit")
    assert report["recovered"] is True and environment == adapter.environment
    assert events == [("fault-cleaned", None), ("new-recovery", None)]


def test_failed_fault_cleanup_never_starts_recovery(controlled, monkeypatch):
    adapter, _messages, _events, run = controlled

    def failed(*_args, **_kwargs):
        raise SandboxUnavailable("fixed lifecycle cleanup failure")

    monkeypatch.setattr(adapter, "_exchange", failed)
    monkeypatch.setattr(adapter, "recover", lambda *_args, **_kwargs: pytest.fail("cannot restart"))
    with pytest.raises(SandboxUnavailable, match="cleanup failure"):
        run(controller_fault="controller-exit")


def test_controller_exchange_stops_and_removes_before_fresh_recovery(controlled, monkeypatch):
    adapter, messages, events, run = controlled
    messages[-1] = {"event": "fault-armed"}
    messages.extend(
        [
            copy.deepcopy(messages[0]),
            copy.deepcopy(messages[1]),
            {"event": "recovered", "recovered": True},
        ]
    )

    def observe(channel, service, request, *, started, deadline, on_tick):
        assert request["event"] == "check-controller"
        assert 55 < deadline - time.monotonic() <= 60
        assert channel.receive() == {"event": "fault-armed"}
        events.append(("fault-observed", service))
        return {
            "name": request["name"],
            "challenge": request["challenge"],
            "independent_stop": True,
        }

    monkeypatch.setattr(gpu_adapter, "observe_fault", observe)
    report, _environment = run(controller_fault="controller-exit")
    assert report["recovered"] is True
    stops = [i for i, event in enumerate(events) if event == ("stop", None)]
    starts = [i for i, event in enumerate(events) if event[0] == "start"]
    assert len(stops) == len(starts) == 2 and starts[0] < stops[0] < starts[1] < stops[1]
    assert events.count(("send", {"event": "finish"})) == 1
    assert list(adapter.directory.iterdir()) == []


@pytest.mark.parametrize("seconds", [-1, 60, 150, 181, float("inf"), float("nan")])
def test_controller_fault_cannot_consume_recovery_budget_or_get_unbounded_time(controlled, seconds):
    _adapter, _messages, events, run = controlled
    with pytest.raises((ValueError, SandboxUnavailable)):
        run(controller_fault="controller-exit", controller_seconds=seconds)
    assert events == []


@pytest.mark.parametrize("purpose", ["task", "recovery"])
def test_controller_faults_need_a_maintenance_service(controlled, purpose):
    _adapter, _messages, events, run = controlled
    with pytest.raises(ValueError, match="maintenance"):
        run(controller_fault="controller-exit", purpose=purpose)
    assert events == []


def test_private_source_follows_verified_preparation_and_final_result_follows_stop(controlled):
    adapter, _messages, events, run = controlled
    output, environment = run()
    assert output == result() and environment == adapter.environment
    start = next(value for name, value in events if name == "start")
    assert "synthetic-source-never-executed" not in repr(start)
    assert "hidden-name" not in repr(start)
    execution = next(
        i
        for i, (name, value) in enumerate(events)
        if name == "send" and value["event"] == "execute"
    )
    assert events.index(("receive", "prepared")) < events.index(("begin", None)) < execution
    assert events.index(("eof", None)) < events.index(("stop", None))
    assert events[-1] == ("observe", False)
    assert not list(adapter.directory.iterdir())


def test_startup_maintenance_sends_no_learner_input_and_waits_for_cleanup(controlled):
    adapter, messages, events, run = controlled
    messages[-1] = {"event": "recovered", "recovered": True}
    assert run(maintenance=True) == adapter.environment
    requests = [value for name, value in events if name == "send"]
    assert requests == [
        {"event": "prepare", "target": TARGET},
        {"event": "recover"},
        {"event": "finish"},
    ]
    assert not any(name == "begin" for name, _value in events)
    assert events.index(("eof", None)) < events.index(("stop", None))
    assert events[-1] == ("observe", False)
    assert not list(adapter.directory.iterdir())


@pytest.mark.parametrize(
    "response",
    [
        {"event": "completed", "result": result(), "recovered": True},
        {"event": "recovered", "result": result(), "recovered": True},
        {"event": "recovered", "recovered": False},
        {"event": "recovered", "recovered": 1},
    ],
)
def test_startup_maintenance_rejects_scores_or_unconfirmed_recovery(controlled, response):
    _adapter, messages, events, run = controlled
    messages[-1] = response
    with pytest.raises(SandboxUnavailable, match="startup recovery"):
        run(maintenance=True)
    assert ("stop", None) in events
    assert ("send", {"event": "finish"}) not in events


@pytest.mark.parametrize("maintenance,purpose", [(True, "task"), (False, "maintenance")])
def test_task_and_maintenance_services_cannot_be_interchanged(controlled, maintenance, purpose):
    _adapter, _messages, events, run = controlled
    with pytest.raises(ValueError, match="service instance"):
        run(maintenance=maintenance, purpose=purpose)
    assert not events


def test_resource_checks_bind_fresh_host_listener_and_complete_cleanup(controlled):
    import socket

    from tests.test_gpu_isolation import resource_report

    adapter, messages, events, run = controlled
    observed = {}

    def response():
        request = next(
            value
            for name, value in events
            if name == "send" and value["event"] == "check-resources"
        )
        assert set(request) == {"event", "canary", "port"}
        with socket.create_connection(("127.0.0.1", request["port"]), timeout=1):
            pass
        observed.update(request)
        return {
            "event": "resources-checked",
            "report": resource_report(request, TARGET),
            "recovered": True,
        }

    messages[-1] = response
    report, environment = run(resource_check=True)
    assert report["gpu"] == TARGET["uuid"] and environment == adapter.environment
    assert not any(name == "begin" for name, _value in events)
    assert events[-1] == ("observe", False)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", observed["port"]), timeout=1)


def test_resource_checks_reject_partial_report_even_with_recovered_flag(controlled):
    _adapter, messages, events, run = controlled
    messages[-1] = {"event": "resources-checked", "report": {"passed": True}, "recovered": True}
    with pytest.raises(SandboxUnavailable, match="Incomplete resource"):
        run(resource_check=True)
    assert ("stop", None) in events
    assert ("send", {"event": "finish"}) not in events


@pytest.mark.parametrize("stage", [0, 1])
def test_wrong_preparation_never_receives_source(controlled, stage):
    _adapter, messages, events, run = controlled
    messages[stage]["unexpected"] = True
    with pytest.raises(SandboxUnavailable):
        run()
    assert not any(name == "send" and value["event"] == "execute" for name, value in events)
    assert ("stop", None) in events


@pytest.mark.parametrize("stage", [0, 1])
def test_boolean_is_not_an_integer_in_controller_identity(controlled, stage):
    _adapter, messages, events, run = controlled
    if stage == 0:
        messages[0]["protocol"] = True
    else:
        messages[1]["target"]["index"] = False
    with pytest.raises(SandboxUnavailable):
        run()
    assert not any(name == "send" and value["event"] == "execute" for name, value in events)


def test_cancelled_begin_does_not_send_source(controlled):
    _adapter, _messages, events, run = controlled

    def cancel():
        raise SandboxUnavailable("fixed cancellation")

    with pytest.raises(SandboxUnavailable, match="cancellation"):
        run(on_begin=cancel)
    assert not any(name == "send" and value["event"] == "execute" for name, value in events)


def test_unconfirmed_device_recovery_discards_completed_result(controlled):
    _adapter, messages, events, run = controlled
    messages[-1]["recovered"] = False
    with pytest.raises(SandboxUnavailable, match="device recovery"):
        run()
    assert ("stop", None) in events


def test_external_process_after_completion_still_discards_result(controlled, monkeypatch):
    _adapter, _messages, events, run = controlled
    idle_checks = 0

    def occupied(_self, service=None):
        nonlocal idle_checks
        if service is None:
            idle_checks += 1
            if idle_checks == 2:
                raise SandboxUnavailable("fixed external process")

    monkeypatch.setattr(gpu_adapter.DeviceMonitor, "observe", occupied)
    with pytest.raises(SandboxUnavailable, match="external"):
        run()
    assert ("stop", None) in events


def test_unverified_service_stop_never_returns_a_result(controlled, monkeypatch):
    _adapter, _messages, _events, run = controlled

    def failed(_self):
        raise SandboxUnavailable("fixed stop failure")

    monkeypatch.setattr(OwnedJobService, "stop", failed)
    with pytest.raises(SandboxUnavailable, match="stop failure"):
        run()


def test_changed_configuration_is_rejected_before_device_or_service_work(controlled):
    adapter, _messages, events, run = controlled
    adapter.target["model"] = "changed device identity"
    with pytest.raises(SandboxUnavailable, match="configuration changed"):
        run()
    assert not events


@pytest.mark.parametrize("defect", ["hidden", "extra", "count", "accepted", "performance", "nan"])
def test_invalid_or_private_result_fields_are_rejected(defect):
    value = result()
    if defect == "hidden":
        value["public_cases"][0]["name"] = "hidden-name"
    elif defect == "extra":
        value["source"] = "private code"
    elif defect == "count":
        value["passed"] = True
    elif defect == "accepted":
        value.update(verdict="accepted", label=LABELS["accepted"])
    elif defect == "performance":
        value["performance"] = {}
    else:
        value["compile_initialization_ms"] = float("nan")
    with pytest.raises(SandboxUnavailable):
        validate_result(value, problem(), "run")


@pytest.mark.parametrize(
    "key,value",
    [
        ("uuid", "all"),
        ("index", True),
        ("architecture", [8, -1]),
        ("driver", "unknown"),
        ("model", ""),
    ],
)
def test_target_identity_cannot_be_implicit_or_ambiguous(key, value):
    target = copy.deepcopy(TARGET)
    target[key] = value
    with pytest.raises(ValueError):
        validate_target(target)


@pytest.mark.parametrize("defect", [None, "missing", "median", "guest-time", "extra", "nonfinite"])
def test_submit_requires_complete_controller_performance_samples(defect):
    value = result()
    value.update(verdict="accepted", label=LABELS["accepted"], passed=2, total=2)
    value["public_cases"][0].update(verdict="accepted", label=LABELS["accepted"])
    value["performance"] = {
        "profile": PROFILE["id"],
        "profile_digest": profile_digest(),
        "measurement": PROFILE["measurement"],
        "warmup_batches": PROFILE["warmup_batches"],
        "sample_batches": PROFILE["sample_batches"],
        "batch_repetitions": PROFILE["batch_repetitions"],
        "samples_ms": [1.0, 2.0, 3.0, 4.0, 5.0],
        "median_batch_ms": 3.0,
    }
    if defect == "missing":
        del value["performance"]
    elif defect == "median":
        value["performance"]["median_batch_ms"] = 2.0
    elif defect == "guest-time":
        value["performance"]["measurement"] = "guest-reported-time"
    elif defect == "extra":
        value["performance"]["private_inputs"] = [1, 2]
    elif defect == "nonfinite":
        value["performance"]["samples_ms"][0] = float("inf")
    if defect is None:
        validate_result(value, problem(), "submit")
    else:
        with pytest.raises(SandboxUnavailable):
            validate_result(value, problem(), "submit")
