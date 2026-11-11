"""Modeled device/task execution for adapter boundaries; never GPU acceptance."""

import copy
import time
from types import SimpleNamespace

import pytest

from judge import container_adapter as module
from judge.container_environment import validate_environment
from judge.container_execution import KernelTask
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_receipt import binding
from judge.sandbox import SandboxUnavailable
from tests.gpu_evidence_fixtures import result_fixture
from tests.test_container_driver import bundle as bundle
from tests.test_gpu_adapter import TARGET, problem


@pytest.fixture
def configured(monkeypatch, bundle):
    events, tasks = [], []
    management = {
        "schema": 2,
        "profile": "nvidia-inventory-v2",
        "driver_version": TARGET["driver"],
        "library": {
            "name": "libnvidia-ml.so." + TARGET["driver"],
            "soname": "libnvidia-ml.so.1",
            "size": 1,
            "sha256": "a" * 64,
        },
        "utility": {"name": "nvidia-smi", "size": 1, "sha256": "b" * 64},
    }
    selection = {
        "schema": 1,
        "profile": "single-cuda-device-v1",
        "uuid": TARGET["uuid"],
        "driver": TARGET["driver"],
        "minor": 0,
        "uvm_major": 509,
    }
    engine = {
        "ServerVersion": "29.9.2",
        "KernelVersion": "6.12.0-test",
        "OSType": "linux",
        "Architecture": "x86_64",
    }
    control = module.ContainerController.__new__(module.ContainerController)
    control.guard = object()
    control.engine = SimpleNamespace(verify=lambda: copy.deepcopy(engine))
    control.saved = {
        "version": 4,
        "device_observer": {"target": copy.deepcopy(TARGET), "library": management},
    }
    monkeypatch.setattr(module.ContainerController, "check", lambda self: events.append("checked"))

    class Monitor:
        def __init__(self, value):
            self.saved = copy.deepcopy(value.saved["device_observer"])

        def task_device_spec(self):
            return copy.deepcopy(selection)

    monkeypatch.setattr(module, "ContainerDeviceMonitor", Monitor)
    adapter = module.ContainerGPUAdapter(
        control, image="sha256:" + "c" * 64, driver_manifest=bundle[1]
    )
    state = SimpleNamespace(
        adapter=adapter,
        control=control,
        events=events,
        tasks=tasks,
        selection=selection,
        engine=engine,
    )

    def create(task, original, job, reservation, *, image, driver_manifest, purpose):
        task.control = original
        task.target = copy.deepcopy(TARGET)
        task.service = SimpleNamespace(
            image=image,
            driver_manifest=copy.deepcopy(driver_manifest),
            purpose=purpose,
            job_id=job,
            reservation_token=reservation,
        )
        task.recover = lambda: events.append("original-cleanup")
        tasks.append(task)

    def run(task, request, *, deadline, on_tick, on_begin):
        assert task.service.purpose == ("maintenance" if request["event"] == "recover" else "task")
        on_tick()
        on_begin()
        on_tick()
        events.append("runtime-recovery-and-cleanup")
        return {
            "result": result_fixture(request.get("mode", "run")),
            "artifacts": {
                "image": task.service.image,
                "task_contract": adapter.environment["artifacts"]["task_contract"],
                "resource_profile": adapter.environment["resource_profile"],
                "limits": copy.deepcopy(adapter.environment["workload_limits"]),
            },
        }

    state.run = run
    monkeypatch.setattr(KernelTask, "__init__", create)
    monkeypatch.setattr(KernelTask, "run", lambda *args, **kwargs: state.run(*args, **kwargs))
    return state


def evaluate(state, **kwargs):
    adapter = state.adapter
    task = adapter.task(state.control.guard, "job", "reservation")
    return adapter.evaluate(
        task,
        deadline=100,
        problem=problem(),
        source="never executed",
        mode="run",
        on_tick=lambda: None,
        on_begin=kwargs.pop("on_begin", lambda: None),
        **kwargs,
    )


def test_environment_binds_image_device_driver_engine_and_whole_task_budget(configured):
    result, environment = evaluate(configured)
    adapter = configured.adapter
    assert result["verdict"] == "accepted" and "performance" not in result
    assert environment["backend"] == "gvisor-gpu-container"
    assert environment["resource_scope"] == "task-container-including-supervisor-v1"
    assert environment["device_spec"] == configured.selection
    assert environment["artifacts"]["task_image"] == adapter.image
    assert canonical_digest(environment) == adapter.fingerprint
    environment["target"]["uuid"] = "changed caller copy"
    assert adapter.environment["target"] == TARGET


@pytest.mark.parametrize("damage", ["engine", "driver", "image", "observer", "source", "saved"])
def test_environment_change_prevents_task_creation(configured, monkeypatch, damage):
    state = configured
    if damage == "engine":
        state.engine["ServerVersion"] = "29.9.3"
    elif damage == "driver":
        next(iter(state.adapter.driver_manifest["libraries"].values()))["sha256"] = "d" * 64
    elif damage == "image":
        state.adapter.image = "sha256:" + "d" * 64
    elif damage == "observer":
        state.control.saved["device_observer"]["library"]["utility"]["sha256"] = "d" * 64
    elif damage == "source":
        monkeypatch.setattr(module, "execution_digest", lambda: "d" * 64)
    else:
        state.adapter.environment["artifacts"]["task_contract"] = "d" * 64
    with pytest.raises(SandboxUnavailable, match="environment changed"):
        evaluate(state)
    assert not state.tasks


@pytest.mark.parametrize("damage", ["engine", "result-identity"])
def test_post_cleanup_environment_change_withholds_result(configured, damage):
    original = configured.run

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if damage == "engine":
            configured.engine["KernelVersion"] = "6.13.0-test"
        else:
            value["artifacts"]["task_contract"] = "f" * 64
        return value

    configured.run = changed
    with pytest.raises(SandboxUnavailable, match="environment"):
        evaluate(configured)
    assert "runtime-recovery-and-cleanup" in configured.events


def test_original_lock_and_task_object_are_required(configured):
    with pytest.raises(ValueError, match="original device lock"):
        configured.adapter.task(object(), "job", "reservation")
    with pytest.raises(ValueError, match="original configured kernel"):
        configured.adapter.recover(object(), deadline=100, on_tick=lambda: None)
    assert not configured.tasks


def test_changed_environment_during_start_callback_prevents_execution(configured):
    def change():
        configured.engine["ServerVersion"] = "29.9.3"

    with pytest.raises(SandboxUnavailable, match="environment changed"):
        evaluate(configured, on_begin=change)
    assert "runtime-recovery-and-cleanup" not in configured.events


def test_maintenance_reuses_kernel_recovery_without_publishing_ready(configured):
    adapter = configured.adapter
    task = adapter.task(configured.control.guard, "job", "reservation", purpose="maintenance")
    assert adapter.recover(task, deadline=100, on_tick=lambda: None) == adapter.environment
    assert "ready" not in adapter.environment
    # A synthetic lock cannot turn this component fixture or its saved identity
    # into an operational startup receipt.
    with pytest.raises(ValueError, match="readiness"):
        binding(configured.control.guard, "synthetic-worker", adapter)


def test_adapter_named_check_sends_only_fixed_name_and_fresh_challenge(configured):
    from tests.test_container_kernel_checks import check_report

    requests = []

    def run(task, request, *, deadline, on_tick, on_begin):
        assert task.service.purpose == "maintenance" and deadline == 100
        assert set(request) == {"event", "name", "challenge"}
        assert request["event"] == "check-kernel" and request["name"] == "public-run"
        requests.append(request)
        on_tick()
        on_begin()
        return {
            "report": check_report(request),
            "artifacts": {
                "image": task.service.image,
                "task_contract": configured.adapter.environment["artifacts"]["task_contract"],
                "resource_profile": configured.adapter.environment["resource_profile"],
                "limits": copy.deepcopy(configured.adapter.environment["workload_limits"]),
            },
        }

    configured.run = run
    adapter = configured.adapter
    for _ in range(2):
        task = adapter.task(configured.control.guard, "job", "reservation", purpose="maintenance")
        report, environment = adapter.check_kernel(
            task, "public-run", deadline=100, on_tick=lambda: None
        )
        assert report["name"] == "public-run" and environment == adapter.environment
    assert requests[0]["challenge"] != requests[1]["challenge"]
    assert "ready" not in environment


def test_resource_adapter_uses_fresh_challenge_and_original_maintenance_task(configured):
    from tests.test_container_isolation import resource_report

    requests = []

    def run(task, request, *, deadline, on_tick, on_begin):
        assert task.service.purpose == "maintenance" and deadline == 180
        assert set(request) == {"event", "canary"} and request["event"] == "check-resources"
        requests.append(request)
        on_tick()
        on_begin()
        return {
            "report": resource_report(request, gpu=True),
            "artifacts": {
                "image": task.service.image,
                "task_contract": configured.adapter.environment["artifacts"]["task_contract"],
                "resource_profile": configured.adapter.environment["resource_profile"],
                "limits": copy.deepcopy(configured.adapter.environment["workload_limits"]),
            },
        }

    configured.run = run
    adapter = configured.adapter
    for _ in range(2):
        task = adapter.task(configured.control.guard, "job", "reservation", purpose="maintenance")
        report, environment = adapter.check_resources(task, deadline=180, on_tick=lambda: None)
        assert report["gpu"] == adapter.target["uuid"] and environment == adapter.environment
        assert report["challenge"] == requests[-1]["canary"]
        report["checks"].clear()
        assert environment.get("ready") is None
    assert requests[0]["canary"] != requests[1]["canary"]


@pytest.mark.parametrize("clock_start", [None, 934.0000000000003])
@pytest.mark.parametrize("defect", [None, "old-cleanup", "new-recovery", "reuse"])
def test_controller_fault_creates_recovery_only_after_original_cleanup(
    configured, defect, clock_start, monkeypatch
):
    from tests.test_container_lifecycle import cleanup, report

    if clock_start is not None:
        monkeypatch.setattr(module.time, "monotonic", lambda: clock_start)
    events = []
    adapter = configured.adapter
    old = adapter.task(configured.control.guard, "job", "reservation", purpose="maintenance")

    def run(task, request, *, deadline, on_tick, on_begin):
        assert task.service.purpose == "maintenance"
        assert task.service.job_id == old.service.job_id
        assert task.service.reservation_token == old.service.reservation_token
        on_tick()
        on_begin()
        if request["event"] == "check-controller":
            assert task is old and len(configured.tasks) == 1
            assert set(request) == {"event", "name", "challenge"}
            events.append("old-cleaned")
            value = report(request, device=TARGET["uuid"])
            if defect == "old-cleanup":
                value["cleanup"]["removed"] = False
            completed = {"report": value}
        else:
            assert request == {"event": "recover"} and task is not old and events == ["old-cleaned"]
            events.append("new-recovery")
            if defect == "new-recovery":
                raise SandboxUnavailable("fixed fresh recovery failure")
            task.service.container_id = "c" * 64 if defect == "reuse" else "d" * 64
            task.status = {"nonce": "c" * 32}
            task.cleanup = cleanup(task.service.container_id, task.status["nonce"], TARGET["uuid"])
            completed = {}
        completed["artifacts"] = {
            "image": task.service.image,
            "task_contract": adapter.environment["artifacts"]["task_contract"],
            "resource_profile": adapter.environment["resource_profile"],
            "limits": copy.deepcopy(adapter.environment["workload_limits"]),
        }
        return completed

    configured.run = run

    def check():
        return adapter.check_controller(
            old, "controller-exit", deadline=time.monotonic() + 180, on_tick=lambda: None
        )

    if defect:
        with pytest.raises(SandboxUnavailable):
            check()
        if defect == "old-cleanup":
            assert len(configured.tasks) == 1 and events == ["old-cleaned"]
    else:
        value, environment = check()
        assert events == ["old-cleaned", "new-recovery"] and value["recovered"] is True
        assert environment == adapter.environment and "ready" not in environment


@pytest.mark.parametrize("seconds", [-1, 20, 125, 181, float("nan")])
def test_controller_fault_keeps_budget_for_independent_recovery(configured, seconds):
    adapter = configured.adapter
    task = adapter.task(configured.control.guard, "job", "reservation", purpose="maintenance")
    with pytest.raises((ValueError, SandboxUnavailable)):
        adapter.check_controller(
            task, "service-deadline", deadline=time.monotonic() + seconds, on_tick=lambda: None
        )
    assert len(configured.tasks) == 1 and "runtime-recovery-and-cleanup" not in configured.events


def test_controller_fault_rejects_next_float_after_original_deadline(configured, monkeypatch):
    import math

    now = 934.0000000000003
    monkeypatch.setattr(module.time, "monotonic", lambda: now)
    task = configured.adapter.task(
        configured.control.guard, "job", "reservation", purpose="maintenance"
    )
    with pytest.raises(ValueError, match="bounded maintenance"):
        configured.adapter.check_controller(
            task,
            "controller-exit",
            deadline=math.nextafter(now + 180, math.inf),
            on_tick=lambda: None,
        )
    assert len(configured.tasks) == 1 and "runtime-recovery-and-cleanup" not in configured.events


def test_saved_container_identity_never_hashes_live_source(configured, monkeypatch):
    env = copy.deepcopy(configured.adapter.environment)
    env["execution_digest"] = "0" * 64
    monkeypatch.setattr(module, "execution_digest", lambda: pytest.fail("No live history check"))
    assert validate_environment(env, canonical_digest(env))["id"] == "cuda-session-v1"


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "backend",
        "protocol",
        "margin-bool",
        "scope",
        "limits",
        "selection",
        "profile",
        "driver",
        "management",
        "engine",
        "hash",
        "compiler",
        "controller-budget",
    ],
)
def test_invalid_saved_identity_is_rejected_even_after_rehash(configured, damage):
    env = copy.deepcopy(configured.adapter.environment)
    artifacts = env["artifacts"]
    if damage == "extra":
        env["ready"] = True
    elif damage == "backend":
        env["backend"] = "ordinary-docker"
    elif damage == "protocol":
        env["controller_protocol"] = True
    elif damage == "margin-bool":
        env["controller_margin_seconds"] = True
    elif damage == "scope":
        env["resource_scope"] = "guest-only"
    elif damage == "limits":
        env["workload_limits"]["memory.max"] = "1073741824"
    elif damage == "selection":
        env["device_spec"]["minor"] = True
    elif damage == "profile":
        env["session_profile"]["sample_batches"] = 4
    elif damage == "driver":
        artifacts["driver_manifest"]["libraries"].popitem()
    elif damage == "management":
        artifacts["management_manifest"].pop("utility")
    elif damage == "engine":
        artifacts["engine"]["architecture"] = "arm64"
    elif damage == "hash":
        artifacts["task_image"] = "latest"
    elif damage == "compiler":
        artifacts["nvrtc_libraries"].popitem()
    else:
        artifacts["controller_limits"]["pids.max"] = "512"
    with pytest.raises(ValueError):
        validate_environment(env, canonical_digest(env))
