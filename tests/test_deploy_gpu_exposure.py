"""Real private files/locks/SIGKILL; modeled Docker, proxy, DB and GPU admission."""

import copy
import hashlib
import json
import os
import signal
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts import deploy
from scripts.deploy_gpu_exposure import GPUExposureError, validate_record
from scripts.deploy_gpu_readiness import GPUReadiness
from scripts.deploy_gpu_worker import WorkerLifecycleError
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_candidate import preparation as preparation
from tests.test_deploy_gpu_check import candidate as candidate
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import state
from tests.test_deploy_gpu_readiness import readiness as readiness
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.fixture
def exposure(preparation, monkeypatch):
    p = preparation
    p.run()
    item, model = p.value.item, p.value.model
    result = SimpleNamespace(
        preparation=p,
        item=item,
        model=model,
        failure=None,
        hook=lambda event: None,
        alter_status=lambda value: None,
        alter_snapshot=lambda value: None,
    )
    model.change(
        lambda data: data.update(
            exposure={
                "pause": True,
                "port": None,
                "web_port": 18001,
                "events": [],
                "writes": [],
            }
        )
    )

    def saved():
        return model.read()["exposure"]

    def change(**fields):
        model.change(lambda data: data["exposure"].update(fields))
        item.model.port, item.model.pause = saved()["port"], saved()["pause"]

    def event(name):
        model.change(lambda data: data["exposure"]["events"].append(name))
        result.hook(name)

    def record():
        records = state(item)["gpu_exposures"].values()
        round_id = p.record()["attempts"][-1]["round_id"]
        return next(row for row in records if row["round_id"] == round_id)

    def snapshot(*args):
        value = item.database.gpu_worker_snapshot(*args)
        value["admission"] = "paused" if saved()["pause"] else "open"
        result.alter_snapshot(value)
        return value

    monkeypatch.setattr(item.database, "gpu_exposure_snapshot", snapshot)

    def coordinator():
        host = p.coordinator()
        host.port = lambda gen: saved()["web_port"]
        host.proxy.probe = lambda: {"port": saved()["port"]}

        def control(generation, *command, **kwargs):
            assert generation == item.generation and command[0] == "judge_admission"
            if command[1] == "resume":
                assert command[2:] == ("--token", item.token)
                # Inspect durable bytes BEFORE allowing the simulated write window.
                journal = state(item)
                assert journal["current"] == generation
                assert journal["operation"]["exposure_target"] == generation["id"]
                assert record()["phase"] == "decided"
                if result.failure == "before-resume":
                    raise RuntimeError("modeled resume refusal")
                change(pause=False)
                event("resume")
                if result.failure == "resume":
                    raise RuntimeError("modeled lost resume acknowledgement")
                return b"{}"
            assert command == ("judge_admission", "status") and kwargs == {"timeout": 30}
            judge = generation["manifest"]["judge"]
            fingerprint = hashlib.sha256(
                f"wasi:{judge['bundle_sha256']}:{judge['policy_sha256']}".encode()
            ).hexdigest()
            value = {
                "admission": "paused" if saved()["pause"] else "open",
                "token": item.token if saved()["pause"] else None,
                "paused_at": "2026-11-02T00:00:00+00:00" if saved()["pause"] else None,
                "worker_available": True,
                "gpu_configured": True,
                "gpu_worker_available": True,
                "fingerprint": fingerprint,
                "incompatible_pending": 0,
                "incompatible_gpu_reservations": 0,
                "queued": 0,
                "running": 0,
                "gpu_reserved": 0,
            }
            result.alter_status(value)
            return json.dumps(value).encode()

        def activate(port):
            assert record()["phase"] == "activating" and port == record()["port"]
            change(port=port)
            # User data accepted on this decided database cannot be rolled back.
            model.change(
                lambda data: data["exposure"]["writes"].append(item.generation["database"])
            )
            event("proxy")
            if result.failure == "proxy":
                raise RuntimeError("modeled lost proxy acknowledgement")

        def health(generation, *, external, require_gpu):
            assert generation == item.generation and external and require_gpu
            assert saved()["port"] == saved()["web_port"]
            event("health")
            if result.failure == "health":
                raise RuntimeError("modeled unavailable GPU health")

        host.control, host.proxy.activate, host.health = control, activate, health
        return host

    def run(phase="active"):
        host = coordinator()
        with host.locked():
            return host.expose_gpu(item.generation, p.value.release, phase=phase)

    result.run, result.coordinator, result.change = run, coordinator, change
    result.saved, result.record = saved, record
    return result


def test_exposure_persists_database_before_resume_and_keeps_it_on_retry(exposure):
    value = exposure
    original_events = value.preparation.events()
    result = value.run()
    assert result["database"] == value.item.generation["database"]
    assert value.record()["phase"] == state(value.item)["operation"]["phase"] == "active"
    assert value.saved()["events"] == ["resume", "proxy", "health"]
    writes = value.saved()["writes"]
    assert value.run() == result
    assert value.saved()["events"] == ["resume", "proxy", "health", "health"]
    assert value.saved()["writes"] == writes
    assert value.preparation.events() == original_events
    assert len(value.preparation.candidate.calls()) == 2


@pytest.mark.parametrize("failure", ["before-resume", "resume", "proxy", "health"])
def test_lost_ack_or_failure_never_selects_old_database_or_restarts_worker(exposure, failure):
    value = exposure
    before = value.preparation.events()
    value.failure = failure
    with pytest.raises(RuntimeError, match="modeled"):
        value.run()
    journal = state(value.item)
    assert journal["current"] == value.item.generation
    assert journal["operation"]["exposure_target"] == value.item.generation["id"]
    writes = value.saved()["writes"]
    value.failure = None
    value.run()
    assert value.saved()["events"].count("resume") == 1
    assert value.saved()["events"].count("proxy") == 1
    assert value.preparation.events() == before
    if writes:
        assert value.saved()["writes"] == writes


@pytest.mark.parametrize("defect", ["missing", "unfinished", "round", "archive"])
def test_exposure_cannot_start_or_bypass_candidate_preparation(exposure, defect):
    value = exposure
    host = value.coordinator()
    with host.locked():
        entries = host.state["gpu_preparations"]
        entry = next(iter(entries.values()))
        if defect == "missing":
            entries.clear()
        elif defect == "unfinished":
            entry["attempts"][-1].update(phase="checking_gpu", round_id=None)
        elif defect == "round":
            entry["attempts"][-1]["round_id"] = str(uuid.uuid4())
        else:
            entry["release"] += "-moved"
        host.save()
    with pytest.raises(RuntimeError):
        value.run()
    assert not state(value.item).get("gpu_exposures")
    assert value.saved()["events"] == [] and value.saved()["pause"]


@pytest.mark.parametrize(
    "defect", ["route", "port", "database", "target", "owner", "startup", "heartbeat"]
)
def test_decided_exposure_refuses_changed_identity_without_undoing_decision(exposure, defect):
    value = exposure
    value.failure = "before-resume"
    with pytest.raises(RuntimeError):
        value.run()
    value.failure = None
    if defect == "route":
        value.change(port=19555)
    elif defect == "port":
        value.change(web_port=19555)
    elif defect in {"database", "target"}:
        host = value.coordinator()
        with host.locked():
            if defect == "database":
                host.state["current"]["database"] = "px_example_old_database"
            else:
                host.state["operation"]["exposure_target"] = "0123456789ab"
            host.save()
    elif defect == "startup":

        def restart(data):
            data["containers"]["/" + value.item.generation["gpu_worker"]]["State"]["StartedAt"] = (
                "2026-11-02T01:00:00Z"
            )

        value.model.change(restart)
    else:

        def alter(snapshot):
            if defect == "owner":
                snapshot["device"]["owner_token"] = uuid.uuid4()
            else:
                snapshot["observed_at"] += timedelta(seconds=31)

        value.alter_snapshot = alter
    before = state(value.item)
    with pytest.raises(RuntimeError):
        value.run()
    assert state(value.item) == before
    assert value.saved()["events"] == [] and value.saved()["pause"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("worker_available", False),
        ("gpu_configured", False),
        ("gpu_worker_available", False),
        ("gpu_worker_available", 1),
        ("incompatible_pending", 1),
        ("incompatible_pending", False),
        ("incompatible_gpu_reservations", 1),
        ("fingerprint", "f" * 64),
        ("queued", -1),
        ("token", "foreign"),
        ("paused_at", "invalid"),
    ],
)
def test_frozen_application_admission_must_confirm_both_workers_and_compatibility(
    exposure, field, bad
):
    value = exposure
    value.alter_status = lambda row: row.update({field: bad})
    with pytest.raises(GPUExposureError, match="confirmed"):
        value.run()
    assert value.record()["phase"] == "decided"
    assert value.saved()["events"] == []


def test_open_retry_allows_compatible_user_queue_and_valid_reserved_gpu(exposure):
    value = exposure
    value.failure = "proxy"
    with pytest.raises(RuntimeError):
        value.run()
    value.failure = None
    reservation = uuid.uuid4()
    job = uuid.uuid4()

    def busy(row):
        row.update(pending=True, reservations=True)
        row["device"].update(state="reserved", job_id=job, reservation_token=reservation)

    value.alter_snapshot = busy
    value.alter_status = lambda row: row.update(queued=3, running=1, gpu_reserved=1)
    writes = value.saved()["writes"]
    value.run()
    assert value.saved()["writes"] == writes
    assert value.record()["phase"] == "active"
    assert value.saved()["events"].count("resume") == value.saved()["events"].count("proxy") == 1
    # A healthy-looking device row alone cannot replace formal lease validation.
    value.alter_status = lambda row: row.update(gpu_worker_available=False)
    with pytest.raises(GPUExposureError, match="confirmed"):
        value.run()
    assert value.saved()["writes"] == writes


def test_numeric_job_cannot_be_mistaken_for_the_original_uuid_reservation(exposure):
    value = exposure
    value.run()

    def invalid(row):
        row.update(pending=True, reservations=True)
        row["device"].update(state="reserved", job_id=42, reservation_token=uuid.uuid4())

    value.alter_snapshot = invalid
    with pytest.raises(GPUExposureError, match="device evidence"):
        value.run()


@pytest.mark.parametrize("phase", ["active", "rolled_back"])
def test_original_database_recovery_requires_new_preparation_round_and_keeps_exposure_history(
    exposure, phase
):
    value = exposure
    value.run(phase)
    old = copy.deepcopy(value.record())
    writes = value.saved()["writes"]
    value.change(pause=True, port=None)
    with pytest.raises(GPUExposureError, match="paused again"):
        value.run(phase)
    value.preparation.run(recover=True)
    value.run(phase)
    records = list(state(value.item)["gpu_exposures"].values())
    assert len(records) == 2 and old in records
    assert value.record()["round_id"] != old["round_id"]
    assert value.record()["observation"]["started_at"] != old["observation"]["started_at"]
    assert state(value.item)["current"] == value.item.generation
    assert value.saved()["writes"][: len(writes)] == writes


def test_active_route_withdrawal_requires_recovery_and_original_lifecycle_guards_remain(exposure):
    value = exposure
    value.run()
    with pytest.raises(WorkerLifecycleError, match="maintenance route"):
        value.preparation.value.observe()
    value.change(port=None)
    with pytest.raises(GPUExposureError, match="withdrawn"):
        value.run()
    assert value.saved()["events"].count("proxy") == 1


@pytest.mark.parametrize("defect", ["cpu", "pause", "route"])
def test_verified_preparation_is_not_a_cached_permission_to_decide(exposure, defect):
    value = exposure
    host = value.coordinator()
    if defect == "cpu":
        host.ready = lambda generation: False
    elif defect == "pause":
        value.change(pause=False)
    else:
        value.change(port=19999)
    before = state(value.item)
    with host.locked(), pytest.raises(RuntimeError):
        host.expose_gpu(value.item.generation, value.preparation.value.release)
    assert state(value.item) == before and value.saved()["events"] == []


def test_slow_final_environment_observation_cannot_reuse_expired_heartbeat(exposure, monkeypatch):
    value = exposure
    value.run()
    monotonic, environment = time.monotonic, GPUReadiness.environment
    offset = 0

    def slow(checker, *args):
        nonlocal offset
        result = environment(checker, *args)
        offset += 31
        return result

    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + offset)
    monkeypatch.setattr(GPUReadiness, "environment", slow)
    before = copy.deepcopy(state(value.item))
    with pytest.raises(GPUExposureError, match="identity changed"):
        value.run()
    assert state(value.item) == before and value.saved()["events"].count("proxy") == 1


@pytest.mark.parametrize("defect", ["phase", "schema", "port", "owner", "round", "extra"])
def test_corrupt_exposure_journal_cannot_authorize_mutations(exposure, defect):
    value = exposure
    value.run()
    row = copy.deepcopy(value.record())
    if defect == "owner":
        row["observation"]["owner_token"] = "not-uuid"
    elif defect == "round":
        row["round_id"] = "not-uuid"
    else:
        row[{"extra": "unknown"}.get(defect, defect)] = {
            "phase": "unknown",
            "schema": True,
            "port": True,
            "extra": True,
        }[defect]
    with pytest.raises(GPUExposureError):
        validate_record(row)


@pytest.mark.parametrize(
    "point", ["decided", "resume", "resumed", "activating", "proxy", "health", "active"]
)
def test_sigkill_after_durable_or_external_step_retains_user_writes_and_original_startup(
    exposure, point
):
    value = exposure
    pid = os.fork()
    if pid == 0:
        save = deploy.Deployment.save

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            if any(row["phase"] == point for row in host.state.get("gpu_exposures", {}).values()):
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def hook(event):
            if event == point:
                os.kill(os.getpid(), signal.SIGKILL)

        try:
            value.hook = hook
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", saved)
                value.run()
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 25
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned exposure process did not reach the requested kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        writes = value.saved()["writes"]
        startup = value.record()["observation"]["started_at"]
        value.run()
        assert value.record()["phase"] == "active"
        assert value.record()["observation"]["started_at"] == startup
        assert state(value.item)["current"] == value.item.generation
        if writes:
            assert value.saved()["writes"] == writes
        assert value.saved()["events"].count("resume") == 1
        assert value.saved()["events"].count("proxy") == 1
        assert len(value.preparation.candidate.calls()) == 2
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
