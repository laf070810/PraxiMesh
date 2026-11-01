"""Recovery ordering with real private files and explicit daemon/database models."""

import copy
import os
import signal
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deploy, deploy_gpu_readiness
from scripts.deploy_gpu_readiness import GPUReadinessError
from scripts.deploy_gpu_recovery import GPURecoveryError
from scripts.deploy_gpu_worker import WorkerLifecycleError, WorkerRuntime
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import MANIFEST, Harness
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import state
from tests.test_deploy_gpu_readiness import clock
from tests.test_deploy_gpu_readiness import readiness as readiness
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.mark.parametrize("action", ["apply", "recover", "restore", "expose"])
def test_cpu_lifecycle_cannot_bypass_original_gpu_recovery(tmp_path, monkeypatch, action):
    host = Harness(tmp_path)
    host.state["current"]["manifest"]["gpu"] = {"original": True}
    generation = copy.deepcopy(host.state["current"])
    token = str(uuid.uuid4())
    host.state["operation"] = {
        "id": token,
        "phase": "active" if action == "apply" else "recovering_previous",
        "previous": generation,
        "candidate": copy.deepcopy(host.old),
        "exposure_target": None,
    }
    monkeypatch.setattr(deploy, "verify", lambda *args: copy.deepcopy(MANIFEST))
    original = copy.deepcopy(host.state)
    with pytest.raises(deploy.DeploymentError, match="coordinated GPU lifecycle"):
        if action == "apply":
            host.apply(Path("unused-cpu-release"), "a" * 64)
        elif action == "recover":
            host.recover()
        elif action == "restore":
            host.restore_previous()
        else:
            host.expose(generation, token)
    assert host.events == [] and host.state == original


@pytest.fixture
def recovery(readiness, monkeypatch):
    value = readiness
    original = value.item.database.gpu_worker_snapshot
    initial = datetime.fromisoformat(value.started.replace("Z", "+00:00"))
    timer = clock(monkeypatch, initial.timestamp() + 60)

    def snapshot(*args):
        result = original(*args)
        info = value.model.read()["containers"]["/" + value.item.generation["gpu_worker"]]
        stamp = info["State"]["StartedAt"]
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(seconds=1)
        # Explicit model publication, not actual startup/receipt or CUDA cleanup evidence.
        result["observed_at"] = result["device"]["checked_at"] = observed
        result["device"]["owner_token"] = uuid.uuid5(uuid.NAMESPACE_URL, stamp)
        return result

    monkeypatch.setattr(value.item.database, "gpu_worker_snapshot", snapshot)
    for name in ("freeze", "thaw", "create", "grant", "backup_restore", "invalidate_health"):
        monkeypatch.setattr(
            value.item.database,
            name,
            lambda *a, **kw: pytest.fail("Recovery switched or rewrote a database"),
        )

    def run():
        host = value.item.coordinator()
        with host.locked():
            return host.recover_gpu_worker(value.item.generation, value.release)

    return SimpleNamespace(value=value, run=run, timer=timer)


def record(recovery):
    value = recovery.value
    return state(value.item)["gpu_recoveries"][
        value.item.token + ":" + value.item.generation["gpu_worker"]
    ]


def mutations(recovery):
    return [
        call
        for call in recovery.value.model.read()["calls"]
        if call[:2] in (["container", "create"], ["container", "start"], ["container", "stop"])
    ]


def test_recovery_reuses_original_container_and_database_with_a_new_preflight(recovery):
    value = recovery.value
    initial = state(value.item)
    files = {
        name: (value.item.root / name).read_bytes()
        for name in ("worker.json", "deployment-generation.json")
    }
    before = len(mutations(recovery))
    result = recovery.run()
    intent = record(recovery)
    assert intent["phase"] == "ready" and len(intent["attempts"]) == 1
    assert result["container"] == initial["gpu_runtime"][value.item.generation["gpu_worker"]]["id"]
    assert intent["attempts"][0]["before"] == value.started
    assert result["started_at"] != value.started
    # The completed preflight is already stopped; its reset only journals state.
    assert [call[1] for call in mutations(recovery)[before:]] == ["stop", "start", "start"]
    assert intent["generation"]["database"] == value.item.generation["database"]
    assert all(name == value.item.generation["database"] for name, _, _ in value.calls)
    assert files == {name: (value.item.root / name).read_bytes() for name in files}
    assert state(value.item)["current"] == initial["current"]
    assert not (value.item.root / "receipts").exists()
    before = mutations(recovery)
    assert recovery.run()["started_at"] == result["started_at"]
    assert mutations(recovery) == before


def test_uncleared_physical_reservation_retains_original_journal_and_startup_budget(
    recovery, monkeypatch
):
    value = recovery.value
    value.snapshot["reservations"] = True
    marker = value.item.root / "journal" / "unresolved-original-task.json"
    marker.write_bytes(b"synthetic unresolved original task; not a valid GPU receipt")
    marker.chmod(0o600)
    monkeypatch.setattr(deploy_gpu_readiness, "STARTUP_SECONDS", 1)
    with pytest.raises(GPUReadinessError, match="timed out"):
        recovery.run()
    original = record(recovery)
    before = mutations(recovery)
    with pytest.raises(GPUReadinessError, match="timed out"):
        recovery.run()
    assert record(recovery) == original and mutations(recovery) == before
    assert original["phase"] == "waiting" and len(original["attempts"]) == 1
    assert marker.read_bytes().startswith(b"synthetic unresolved")
    assert value.snapshot["reservations"] is True and value.model.guard is not None


def test_failed_worker_start_retries_only_on_a_later_recovery_call(recovery):
    value = recovery.value
    value.model.change(lambda model: model.update(worker_exits=True))
    with pytest.raises(WorkerLifecycleError, match="exited during startup"):
        recovery.run()
    first = record(recovery)
    assert first["phase"] == "starting" and len(first["attempts"]) == 1
    value.model.change(lambda model: model.update(worker_exits=False))
    result = recovery.run()
    current = record(recovery)
    assert current["phase"] == "ready" and len(current["attempts"]) == 2
    assert current["attempts"][0] == first["attempts"][0]
    assert current["attempts"][1]["started"] == result["started_at"]


def test_exited_start_is_reconciled_after_losing_the_start_confirmation(recovery, monkeypatch):
    value = recovery.value
    value.model.change(lambda model: model.update(worker_exits=True))
    original = WorkerRuntime.confirm_start

    def interrupt(runtime, info):
        if runtime.mode == "worker":
            raise InterruptedError("lost confirmation after daemon start")
        return original(runtime, info)

    with monkeypatch.context() as patch:
        patch.setattr(WorkerRuntime, "confirm_start", interrupt)
        with pytest.raises(InterruptedError):
            recovery.run()
    first = record(recovery)
    before = mutations(recovery)
    runtime = state(value.item)["gpu_runtime"][value.item.generation["gpu_worker"]]
    assert first["phase"] == "starting" and runtime["phase"] == "start_requested"
    with pytest.raises(WorkerLifecycleError, match="exited during startup"):
        recovery.run()
    assert record(recovery) == first and mutations(recovery) == before
    assert (
        state(value.item)["gpu_runtime"][value.item.generation["gpu_worker"]]["phase"]
        == "start_failed"
    )
    value.model.change(lambda model: model.update(worker_exits=False))
    recovery.run()
    assert record(recovery)["phase"] == "ready"
    assert len(record(recovery)["attempts"]) == 2


def test_failed_preflight_can_retry_after_database_repair_without_losing_original_state(recovery):
    value = recovery.value
    value.model.change(lambda model: model.update(check_code=1))
    with pytest.raises(WorkerLifecycleError, match="exited during startup"):
        recovery.run()
    assert record(recovery)["phase"] == "checking"
    value.model.change(lambda model: model.update(check_code=0))
    recovery.run()
    current = record(recovery)
    assert current["phase"] == "ready" and len(current["attempts"]) == 1
    assert len(current["attempts"][0]["failed_checks"]) == 1


def test_exposure_intent_forbids_returning_to_another_database(recovery):
    value = recovery.value
    host = value.item.coordinator()
    with host.locked():
        host.state["operation"]["exposure_target"] = "other-generation"
        host.state["current"] = copy.deepcopy(host.state["operation"]["previous"])
        host.save()
    before = mutations(recovery)
    with pytest.raises(GPURecoveryError, match="before exposure"):
        recovery.run()
    assert mutations(recovery) == before and not state(value.item).get("gpu_recoveries")


def test_missing_original_container_is_not_recreated_during_recovery(recovery):
    value = recovery.value
    value.model.change(
        lambda model: model["containers"].pop("/" + value.item.generation["gpu_worker"])
    )
    before = mutations(recovery)
    with pytest.raises(WorkerLifecycleError, match="missing"):
        recovery.run()
    assert mutations(recovery) == before


def interrupt_after_phase(recovery, monkeypatch, phase):
    original = deploy.Deployment.save

    def save(host, *args, **kwargs):
        result = original(host, *args, **kwargs)
        records = host.state.get("gpu_recoveries", {})
        if any(value["phase"] == phase for value in records.values()):
            raise InterruptedError("modeled recovery interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(deploy.Deployment, "save", save)
        with pytest.raises(InterruptedError):
            recovery.run()


def test_pending_recovery_prevents_configuration_switch(recovery, monkeypatch):
    interrupt_after_phase(recovery, monkeypatch, "resetting_check")
    value = recovery.value
    with pytest.raises(ValueError, match="original GPU recovery"):
        value.item.publish(value.item.generation, enabled=True)
    recovery.run()
    value.act("stop")
    value.item.publish(value.item.generation, enabled=True)


def test_changed_operation_cannot_take_over_pending_recovery(recovery, monkeypatch):
    interrupt_after_phase(recovery, monkeypatch, "resetting_check")
    value = recovery.value
    host = value.item.coordinator()
    value.item.token = str(uuid.uuid4())
    with host.locked():
        host.state["operation"]["id"] = value.item.token
        host.save()
    before = mutations(recovery)
    with pytest.raises(GPURecoveryError, match="original GPU recovery operation"):
        recovery.run()
    assert mutations(recovery) == before


@pytest.mark.parametrize(
    "phase", ["stopping", "resetting_check", "checking", "starting", "waiting", "ready"]
)
def test_sigkill_resumes_original_recovery_without_duplicate_lifecycle_calls(recovery, phase):
    value = recovery.value
    # Docker is a persistent file model, not a worker process surviving fork.
    value.model.guard.close()
    value.model.guard = None
    pid = os.fork()
    if pid == 0:
        original = deploy.Deployment.save

        def save(host, *args, **kwargs):
            result = original(host, *args, **kwargs)
            records = host.state.get("gpu_recoveries", {})
            if any(record["phase"] == phase for record in records.values()):
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", save)
                recovery.run()
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
                pytest.fail("Owned recovery child did not reach interruption")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        original = record(recovery)
        result = recovery.run()
        resumed = record(recovery)
        assert resumed["phase"] == "ready" and len(resumed["attempts"]) == 1
        assert resumed["container"] == original["container"] == result["container"]
        calls = mutations(recovery)
        assert sum(call[1] == "create" for call in calls) == 2
        assert sum(call[1] == "start" for call in calls) == 4
        assert sum(call[1] == "stop" for call in calls) == 1
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.parametrize("field", ["check_id", "check_before", "check_started", "started"])
def test_recovery_phase_requires_its_original_instance_evidence(recovery, monkeypatch, field):
    interrupt_after_phase(recovery, monkeypatch, "waiting")
    value = recovery.value
    host = value.item.coordinator()
    with host.locked():
        key = value.item.token + ":" + value.item.generation["gpu_worker"]
        host.state["gpu_recoveries"][key]["attempts"][-1][field] = None
        host.save()
    before = mutations(recovery)
    with pytest.raises(GPURecoveryError, match="Invalid original GPU recovery attempt"):
        recovery.run()
    assert mutations(recovery) == before


def test_ready_phase_never_substitutes_for_current_readiness(recovery):
    recovery.run()
    before = mutations(recovery)
    recovery.value.snapshot["device"]["state"] = "quarantined"
    with pytest.raises(GPUReadinessError, match="no longer ready"):
        recovery.run()
    assert mutations(recovery) == before


def test_exit_after_readiness_is_not_confirmed_as_recovery_success(recovery, monkeypatch):
    original = deploy_gpu_readiness.GPUReadiness.wait

    def wait(check):
        result = original(check)
        value = recovery.value
        value.model.change(
            lambda model: model["containers"]["/" + value.item.generation["gpu_worker"]][
                "State"
            ].update(Running=False, Pid=0, Status="exited")
        )
        return result

    monkeypatch.setattr(deploy_gpu_readiness.GPUReadiness, "wait", wait)
    with pytest.raises(GPURecoveryError, match="exited before confirmation"):
        recovery.run()
    assert record(recovery)["phase"] == "waiting"


def test_later_recovery_of_a_confirmed_stopped_instance_keeps_both_attempts(recovery):
    first = recovery.run()
    original = copy.deepcopy(record(recovery)["attempts"][0])
    recovery.value.act("stop")
    second = recovery.run()
    attempts = record(recovery)["attempts"]
    assert len(attempts) == 2 and attempts[0] == original
    assert attempts[1]["before"] == first["started_at"]
    assert first["container"] == second["container"]
    assert first["started_at"] != second["started_at"]


@pytest.mark.parametrize(
    "key",
    [
        "gpu_configuration",
        "gpu_runtime",
        "gpu_recoveries",
        "gpu_candidate_checks",
        "gpu_preparations",
        "gpu_workers",
        "gpu_checks",
    ],
)
def test_cpu_release_cannot_ignore_gpu_state_when_current_manifest_is_cpu_only(
    tmp_path, monkeypatch, key
):
    host = Harness(tmp_path)
    host.state[key] = {"original": "retained"}
    monkeypatch.setattr(deploy, "verify", lambda *args: copy.deepcopy(MANIFEST))
    before = copy.deepcopy(host.state)
    with pytest.raises(deploy.DeploymentError, match="coordinated GPU lifecycle"):
        host.apply(Path("unused-cpu-release"), "a" * 64)
    assert host.events == [] and host.state == before
