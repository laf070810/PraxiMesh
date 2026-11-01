"""Actual files/locks and CPU SIGKILL; Docker, PostgreSQL and GPU are models."""

import copy
import json
import os
import signal
import time
from types import SimpleNamespace

import pytest

from judge.gpu_device_lock import DeviceLock, DeviceLockError
from scripts import deploy
from scripts.deploy_database import DeploymentDatabaseError
from scripts.deploy_gpu_quiescence import GPUQuiescenceError
from scripts.deploy_gpu_worker import WorkerLifecycleError
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import remember, state
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.fixture
def quiescence(lifecycle, monkeypatch, tmp_path):
    item, model = lifecycle.item, lifecycle.model
    prefix = f"px-{item.bound.prepared.config['instance']}-{item.generation['id']}"
    item.generation.update(web=prefix + "-web", worker=prefix + "-worker")
    remember(item, item.generation)
    lifecycle.act("check", "check")
    lifecycle.act("create")
    lifecycle.act("start")
    path = tmp_path / "modeled-database.json"
    path.write_text(json.dumps({"frozen": False, "events": []}))
    value = SimpleNamespace(item=item, model=model, path=path, hook=lambda event: None)

    def read():
        return json.loads(path.read_text())

    def event(name):
        current = read()
        current["events"].append(name)
        if name == "database_frozen":
            current["frozen"] = True
        path.write_text(json.dumps(current))
        value.hook(name)

    def locked():
        with pytest.raises(DeviceLockError):
            with DeviceLock(item.root / "locks", item.bound.prepared.device):
                pytest.fail("Freeze lost the actual original device file lock")

    def freeze(name, token):
        assert name == item.generation["database"] and token == item.token
        locked()
        assert not item.model.held  # The production freeze acquires its own DB guard.
        assert read()["events"][-1] == "services_stopped"
        assert not model.read()["containers"]["/" + item.generation["gpu_worker"]]["State"][
            "Running"
        ]
        event("database_frozen")

    def confirm(name):
        assert name == item.generation["database"] and read()["frozen"]
        locked()
        event("freeze_confirmed")

    monkeypatch.setattr(item.database, "freeze_gpu", freeze)
    monkeypatch.setattr(item.database, "_require_frozen", confirm)
    original = item.coordinator

    def coordinator():
        host = original()
        run = host.run

        def inventory(*args, **kwargs):
            containers = model.read()["containers"].values()
            if args == ("container", "ls", "--all", "--quiet", "--no-trunc"):
                return "\n".join(info["Id"] for info in containers).encode()
            if args[:2] == ("container", "inspect"):
                return json.dumps([info for info in containers if info["Id"] in args[2:]]).encode()
            return run(*args, **kwargs)

        def stop(generation):
            assert generation == item.generation and not item.model.held
            locked()
            assert state(item)["gpu_runtime"][generation["gpu_worker"]]["phase"] == "stopped"
            event("services_stopped")

        host.run, host.stop = inventory, stop
        return host

    def run():
        host = coordinator()
        with host.locked():
            host.quiesce(item.generation)

    value.run, value.coordinator, value.read = run, coordinator, read
    return value


def stops(value):
    return [call for call in value.model.read()["calls"] if call[:2] == ["container", "stop"]]


def test_original_gpu_stops_before_services_and_database_under_same_device_lock(quiescence):
    value = quiescence
    original = {
        name: (value.item.root / name).read_bytes()
        for name in ("worker.json", "deployment-generation.json")
    }
    value.run()
    assert value.read() == {
        "frozen": True,
        "events": ["services_stopped", "database_frozen", "freeze_confirmed"],
    }
    first_stops = stops(value)
    assert len(first_stops) == 1
    value.run()  # A lost confirmation never substitutes for fresh checks.
    assert stops(value) == first_stops and len(value.read()["events"]) == 6
    for name, raw in original.items():
        assert (value.item.root / name).read_bytes() == raw
    with DeviceLock(value.item.root / "locks", value.item.bound.prepared.device):
        pass


def test_undrained_database_leaves_gpu_running_to_finish_original_work(quiescence):
    value = quiescence
    value.item.model.drain = False
    with pytest.raises(DeploymentDatabaseError, match="reservation remains"):
        value.run()
    assert not stops(value) and value.read()["events"] == []
    assert value.model.guard is not None


@pytest.mark.parametrize("role", ["web", "worker"])
def test_changed_cpu_service_name_is_refused_before_stopping_gpu(quiescence, role):
    value = quiescence
    changed = copy.deepcopy(value.item.generation)
    changed[role] = "unrecorded-service"
    host = value.coordinator()
    with host.locked(), pytest.raises(GPUQuiescenceError, match="original generation"):
        host.quiesce(changed)
    assert not stops(value) and value.read()["events"] == []


def test_unrecovered_journal_prevents_service_shutdown_and_freeze(quiescence):
    value = quiescence
    path = value.item.root / "journal" / "unrecognized-original-task"
    path.write_bytes(b"preserve original task evidence")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="recovered original task journal"):
        value.run()
    assert path.read_bytes() == b"preserve original task evidence"
    assert len(stops(value)) == 1 and value.read()["events"] == []


@pytest.mark.parametrize(
    "defect", ["unknown_generation", "exposed_other", "wrong_current", "unconfirmed_start"]
)
def test_freeze_refuses_wrong_generation_or_unconfirmed_runtime(quiescence, defect):
    value = quiescence
    host = value.coordinator()
    with host.locked():
        operation = host.state["operation"]
        if defect == "unknown_generation":
            operation["candidate"] = None
        elif defect == "exposed_other":
            operation["exposure_target"] = "b" * 12
        elif defect == "wrong_current":
            operation["exposure_target"] = value.item.generation["id"]
            host.state["current"] = copy.deepcopy(value.item.generation) | {"database": "another"}
        else:
            host.state["gpu_runtime"][value.item.generation["gpu_worker"]]["phase"] = (
                "start_requested"
            )
        host.save()
    with pytest.raises(GPUQuiescenceError):
        value.run()
    assert not stops(value) and value.read()["events"] == []


@pytest.mark.parametrize("stage", ["services_stopped", "database_frozen"])
def test_changed_instance_during_freeze_never_confirms_or_reopens_database(quiescence, stage):
    value = quiescence

    def change(event):
        if event == stage:
            value.model.change(
                lambda data: data["containers"]["/" + value.item.generation["gpu_worker"]][
                    "State"
                ].update(StartedAt="2026-11-01T12:00:59Z")
            )

    value.hook = change
    with pytest.raises((GPUQuiescenceError, WorkerLifecycleError)):
        value.run()
    assert "freeze_confirmed" not in value.read()["events"]
    assert value.read()["frozen"] == (stage == "database_frozen")


@pytest.mark.parametrize("stage", ["services_stopped", "database_frozen"])
def test_failure_preserves_original_state_and_retry_rechecks(quiescence, stage):
    value = quiescence

    def fail(event):
        if event == stage:
            raise RuntimeError("modeled lost acknowledgment")

    value.hook = fail
    with pytest.raises(RuntimeError, match="acknowledgment"):
        value.run()
    assert value.read()["frozen"] == (stage == "database_frozen")
    value.hook = lambda event: None
    value.run()
    assert value.read()["events"][-1] == "freeze_confirmed" and len(stops(value)) == 1


def test_successful_freeze_does_not_cache_a_later_drain_result(quiescence):
    value = quiescence
    value.run()
    before = value.read()
    value.item.model.drain = False
    with pytest.raises(DeploymentDatabaseError):
        value.run()
    assert value.read() == before


@pytest.mark.parametrize(
    "stage",
    ["stop_requested", "stopped", "services_stopped", "database_frozen", "freeze_confirmed"],
)
def test_sigkill_rechecks_original_stopped_worker_and_database(quiescence, stage):
    value = quiescence
    # Only the daemon model owned this lock; no actual worker process exists.
    value.model.guard.close()
    value.model.guard = None
    pid = os.fork()
    if pid == 0:
        save = deploy.Deployment.save

        def killed(event):
            if event == stage:
                os.kill(os.getpid(), signal.SIGKILL)

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            record = host.state.get("gpu_runtime", {}).get(value.item.generation["gpu_worker"])
            if record:
                killed(record["phase"])
            return result

        try:
            value.hook = killed
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", saved)
                value.run()
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 20
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned quiescence subprocess did not reach its kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        value.run()
        assert value.read()["events"][-1] == "freeze_confirmed"
        assert value.read()["frozen"] and len(stops(value)) == 1
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
