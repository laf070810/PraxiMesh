"""Modeled device/task execution for adapter boundaries; never GPU acceptance."""

import copy
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
    # The complete startup suite has not been ported. No type/config shortcut
    # may turn an execution adapter or a saved identity into an operational receipt.
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
