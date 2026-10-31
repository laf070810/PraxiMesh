"""Kernel caller fences with modeled supervision; not hardware evidence."""

import copy
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from judge import container_controller
from judge import container_execution as module
from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_devices import PROFILE
from judge.container_kernel_protocol import PROTOCOL, expected_cleanup
from judge.gpu_task_protocol import task_request
from judge.sandbox import SandboxUnavailable
from tests.test_container_driver import bundle as bundle
from tests.test_gpu_adapter import TARGET, problem, result


@pytest.fixture
def flow(monkeypatch, bundle):
    events = []
    selection = {
        "schema": 1,
        "profile": PROFILE,
        "uuid": TARGET["uuid"],
        "driver": TARGET["driver"],
        "minor": 0,
        "uvm_major": 509,
    }
    message = task_request(problem(), "synthetic caller fixture", "run")
    identity = module.prepared(selection, bundle[1], TARGET)
    completion = {
        "event": "kernel-completed",
        "protocol": PROTOCOL,
        "recovered": True,
        "ready": False,
        "cleanup": expected_cleanup(message),
        "result": result(),
    }
    incoming = [copy.deepcopy(identity), copy.deepcopy(completion)]

    class Journal:
        record = None

        def load(self):
            return self.record

        def recover(self):
            events.append("outer-recovered")
            self.record = None
            return {"container": "c" * 64, "removed": True, "ready": False}

    class Control:
        def __init__(self):
            self.journal = Journal()

        def check(self):
            events.append("control-check")

    class Monitor:
        def __init__(self, control):
            self.control = control
            self.saved = {"target": copy.deepcopy(TARGET)}

        def task_device_spec(self):
            return copy.deepcopy(selection)

        def observe(self, service=None):
            events.append("task-observed" if service is not None else "idle-observed")

    class Service:
        def __init__(
            self, journal, _job, _reservation, *, image, driver_manifest, device_spec, purpose
        ):
            self.journal, self.image, self.device_spec, self.purpose = (
                journal,
                image,
                device_spec,
                purpose,
            )
            self.owner, self.container_id = "owner", None

        def start(self, _command, **kwargs):
            events.append("start")
            kwargs["on_tick"]()
            self.container_id = "c" * 64
            self.journal.record = {
                "owner": self.owner,
                "image": self.image,
                "version": 3,
                "resource_profile": WORKLOAD_PROFILE,
                "device_spec": copy.deepcopy(self.device_spec),
                "container_id": self.container_id,
                "phase": "running",
            }
            return {"limits": resource_limits(WORKLOAD_PROFILE)}

    class Connection:
        def __init__(self, _service, _status, *, on_tick):
            self.channel, self.on_tick = self, on_tick

        def send(self, value):
            self.on_tick()
            events.append(("send", copy.deepcopy(value)))

        def receive(self):
            self.on_tick()
            return incoming.pop(0)

        def finish(self):
            self.on_tick()
            events.append("finished")

        def close(self):
            events.append("connection-closed")

    monkeypatch.setattr(container_controller, "ContainerController", Control)
    monkeypatch.setattr(module, "ContainerDeviceMonitor", Monitor)
    monkeypatch.setattr(module, "GPUContainerService", Service)
    monkeypatch.setattr(module, "ContainerConnection", Connection)
    control = Control()
    task = module.KernelTask(
        control, "job", "reservation", image="sha256:" + "a" * 64, driver_manifest=bundle[1]
    )

    def run(**kwargs):
        return task.run(
            message,
            deadline=100,
            on_begin=kwargs.pop("on_begin", lambda: events.append("begin")),
            on_tick=kwargs.pop("on_tick", lambda: None),
            **kwargs,
        )

    return SimpleNamespace(
        task=task,
        control=control,
        journal=control.journal,
        events=events,
        incoming=incoming,
        message=message,
        run=run,
        Monitor=Monitor,
        Connection=Connection,
    )


def source_sent(flow):
    return any(type(item) is tuple and item[1].get("event") == "execute" for item in flow.events)


def test_result_requires_begin_original_task_cleanup_and_final_idle(flow):
    value = flow.run()
    position = next(
        i
        for i, item in enumerate(flow.events)
        if type(item) is tuple and item[1].get("event") == "execute"
    )
    assert flow.events.index("begin") < position
    assert flow.events.index("connection-closed") < flow.events.index("outer-recovered")
    assert flow.events[-1] == "idle-observed"
    assert value["result"]["verdict"] == "wrong_answer" and value["ready"] is False
    assert value["artifacts"]["resource_profile"] == WORKLOAD_PROFILE
    with pytest.raises(ValueError, match="once"):
        flow.run()


@pytest.mark.parametrize("defect", ["target", "driver", "boolean", "limits", "extra"])
def test_preparation_change_never_receives_source(flow, defect):
    value = flow.incoming[0]
    if defect == "target":
        value["target"]["architecture"] = [8, 0]
    elif defect == "driver":
        value["driver_manifest"] = "e" * 64
    elif defect == "boolean":
        value["device_spec"]["minor"] = False
    elif defect == "limits":
        value["limits"]["cpu.max"] = "max 100000"
    else:
        value["ready"] = True
    with pytest.raises(SandboxUnavailable, match="preparation"):
        flow.run()
    assert not source_sent(flow) and "begin" not in flow.events
    assert "outer-recovered" in flow.events


def test_cancelled_begin_sends_no_source_and_recovers(flow):
    def cancel():
        raise SandboxUnavailable("cancelled")

    with pytest.raises(SandboxUnavailable, match="cancelled"):
        flow.run(on_begin=cancel)
    assert not source_sent(flow) and "outer-recovered" in flow.events


@pytest.mark.parametrize("field", ["owner", "container_id", "device_spec", "phase"])
def test_changed_record_never_sends_source_or_recovers_replacement(flow, field):
    def replace():
        flow.journal.record[field] = "replacement"

    with pytest.raises(SandboxUnavailable, match="ownership"):
        flow.run(on_begin=replace)
    assert not source_sent(flow) and "outer-recovered" not in flow.events


@pytest.mark.parametrize("defect", ["recovered", "cleanup", "ready", "result", "extra"])
def test_incomplete_completion_is_withheld_and_original_container_recovered(flow, defect):
    reply = flow.incoming[1]
    if defect == "cleanup":
        reply["cleanup"].pop()
    elif defect == "result":
        reply["result"]["passed"] = 100
    else:
        reply[defect] = 1
    with pytest.raises(SandboxUnavailable):
        flow.run()
    assert source_sent(flow) and "outer-recovered" in flow.events
    assert ("send", {"event": "finish"}) not in flow.events


def test_cleanup_failure_withholds_completed_result(flow, monkeypatch):
    def fail():
        raise SandboxUnavailable("retained original task")

    monkeypatch.setattr(flow.journal, "recover", fail)
    with pytest.raises(SandboxUnavailable, match="retained"):
        flow.run()
    assert flow.journal.load() is not None


def test_idle_occupancy_after_cleanup_withholds_result(flow, monkeypatch):
    def observe(_self, service=None):
        if "outer-recovered" in flow.events:
            raise SandboxUnavailable("external device occupancy")

    monkeypatch.setattr(flow.Monitor, "observe", observe)
    with pytest.raises(SandboxUnavailable, match="external"):
        flow.run()
    assert flow.journal.load() is None


def test_close_failure_still_recovers_original_task(flow, monkeypatch):
    def fail(_self):
        raise SandboxUnavailable("bridge did not close")

    monkeypatch.setattr(flow.Connection, "close", fail)
    with pytest.raises(SandboxUnavailable, match="bridge"):
        flow.run()
    assert "outer-recovered" in flow.events


def test_invalid_request_and_wrong_purpose_never_start(flow):
    flow.message["extra"] = True
    with pytest.raises(ValueError):
        flow.run()
    assert "start" not in flow.events


def test_recovery_requires_maintenance_purpose(flow):
    flow.message.clear()
    flow.message.update(event="recover")
    with pytest.raises(ValueError, match="purpose"):
        flow.run()
    assert "start" not in flow.events


@pytest.mark.parametrize("maintenance", [False, True])
def test_named_check_requires_maintenance_and_original_cleanup(flow, maintenance):
    from tests.test_container_kernel_checks import REQUEST, check_report

    flow.message.clear()
    flow.message.update(REQUEST)
    if not maintenance:
        with pytest.raises(ValueError, match="purpose"):
            flow.run()
        assert "start" not in flow.events
        return
    flow.task.service.purpose = "maintenance"
    reply = flow.incoming[1]
    reply.pop("result")
    reply.update(report=check_report(), cleanup=expected_cleanup(REQUEST))
    value = flow.run()
    assert value["report"] == check_report() and "result" not in value
    assert "outer-recovered" in flow.events and flow.events[-1] == "idle-observed"


def test_controller_snapshot_imports_kernel_api_without_host_checkout(tmp_path):
    for name in container_controller.CONTROLLER_SOURCES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(container_controller.ROOT / name, target)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from judge.container_execution import KernelTask; "
            "from judge.container_adapter import ContainerGPUAdapter; "
            "from judge.container_gpu import gpu_task_digest; "
            "from judge.gpu_policy import execution_digest; "
            "print(gpu_task_digest()); print(execution_digest())",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    from judge.gpu_policy import execution_digest

    assert result.stdout.decode().splitlines() == [module.gpu_task_digest(), execution_digest()]


@pytest.mark.parametrize(
    "mode",
    [
        "run",
        "submit",
        "recover",
        "check-public-run",
        "check-vector-subtraction",
        "check-cancel",
        "check-attach-death",
        "check-unexpected",
    ],
)
def test_actual_cpu_entry_constructs_each_protocol_before_starting_a_container(mode):
    from judge.container_kernel_protocol import validate_request
    from scripts.container_kernel_acceptance import request_for

    request = request_for(mode)
    assert validate_request(request) == request
    if mode.startswith("check-"):
        assert set(request) == {"event", "name", "challenge"}
        assert request["event"] == "check-kernel"
    with pytest.raises(ValueError):
        request_for("invalid-task-mode")


def test_cpu_acceptance_problem_uses_the_real_task_data_contract():
    from scripts.container_kernel_acceptance import problem as fixture_problem

    request = task_request(fixture_problem(), "CPU fixture only", "submit")
    assert len(request["problem"]["tests"]) == 2
