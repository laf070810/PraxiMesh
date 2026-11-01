"""Real component composition/files with modeled Docker, database, CPU and GPU execution."""

import copy
import json
import os
import signal
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts import deploy
from scripts.deploy_gpu_candidate import GPUPreparationError, validate_record
from scripts.deploy_gpu_check import expected_report, record_key
from scripts.deploy_gpu_worker import WorkerLifecycleError, WorkerRuntime
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_check import candidate as candidate
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import remember, state
from tests.test_deploy_gpu_readiness import readiness as readiness
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.fixture
def preparation(candidate, monkeypatch):
    value = candidate.value
    item, model = value.item, value.model
    prefix = item.generation["gpu_worker"].removesuffix("-gpu-worker")
    item.generation.update(web=prefix + "-web", worker=prefix + "-worker")
    remember(item, item.generation)
    value.act("stop")
    result = SimpleNamespace(
        candidate=candidate, value=value, cpu_failure=False, hook=lambda event: None
    )

    def event(name):
        model.change(lambda data: data.setdefault("preparation_events", []).append(name))
        result.hook(name)

    snapshot = item.database.gpu_worker_snapshot

    def fresh_snapshot(*args):
        data = snapshot(*args)
        info = model.read()["containers"]["/" + item.generation["gpu_worker"]]
        stamp = info["State"]["StartedAt"]
        checked = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(seconds=1)
        data["observed_at"] = data["device"]["checked_at"] = checked
        data["device"]["owner_token"] = uuid.uuid5(uuid.NAMESPACE_URL, stamp)
        return data

    monkeypatch.setattr(item.database, "gpu_worker_snapshot", fresh_snapshot)
    original, command = item.coordinator, WorkerRuntime.command

    def commanded(runtime, *args, **kwargs):
        if args[:2] in (("container", "create"), ("container", "start"), ("container", "stop")):
            event("gpu_" + runtime.mode + ":" + args[1])
        return command(runtime, *args, **kwargs)

    monkeypatch.setattr(WorkerRuntime, "command", commanded)

    def coordinator():
        host = original()
        run, publish = host.run, host.publish_gpu_configuration

        def inventory(*args, **kwargs):
            containers = model.read()["containers"].values()
            if args == ("container", "ls", "--all", "--quiet", "--no-trunc"):
                return "\n".join(info["Id"] for info in containers).encode()
            if args[:2] == ("container", "inspect"):
                return json.dumps([info for info in containers if info["Id"] in args[2:]]).encode()
            return run(*args, **kwargs)

        def service(generation, role):
            assert generation == item.generation and item.model.port is None
            model.change(lambda data: data.setdefault("cpu_services", {}).update({role: True}))
            event("service:" + role)

        def stop(generation):
            assert generation == item.generation
            model.change(lambda data: data.update(cpu_services={}))
            event("stop:cpu_web")

        def replace(generation):
            assert not model.read().get("cpu_services", {}).get("web")
            event("replace:web")
            service(generation, "web")

        def check(generation, token):
            assert token == item.token and generation == item.generation
            assert model.read()["cpu_services"] == {"web": True, "worker": True}
            event("check:cpu")
            if result.cpu_failure:
                raise RuntimeError("modeled CPU candidate failure")

        def publish_configuration(generation, *, enabled):
            event("publish")
            return publish(generation, enabled=enabled)

        host.run, host.service, host.stop, host.replace_web, host.check = (
            inventory,
            service,
            stop,
            replace,
            check,
        )
        host.ready = lambda generation: model.read().get("cpu_services", {}).get("worker", False)
        host.publish_gpu_configuration = publish_configuration
        return host

    def execute(*, recover=False, release=None):
        host = coordinator()
        with host.locked():
            return host.prepare_candidate(
                item.generation, release or value.release, recover=recover
            )

    result.run, result.coordinator = execute, coordinator
    result.events = lambda: model.read().get("preparation_events", [])
    result.record = lambda: state(item)["gpu_preparations"][
        item.token + ":" + item.generation["gpu_worker"]
    ]
    return result


def test_preparation_composes_publication_services_preflight_startup_and_both_checks(preparation):
    value = preparation
    result = value.run()
    record = value.record()
    assert record["attempts"][-1]["phase"] == "verified"
    assert result["verification"]["passed"] and result["verification"]["cleanup_passed"]
    events = value.events()
    sequence = [
        "publish",
        "service:web",
        "service:worker",
        "gpu_check:start",
        "gpu_worker:start",
        "check:cpu",
    ]
    positions = [events.index(item) for item in sequence]
    assert positions == sorted(positions)
    assert len(value.candidate.calls()) == 2
    assert value.value.item.model.port is None and value.value.item.model.pause
    assert state(value.value.item)["operation"].get("exposure_target") is None
    before = value.events()
    assert value.run()["verification"] == result["verification"]
    assert value.events() == before and len(value.candidate.calls()) == 2


@pytest.mark.parametrize("pending_retirement", [False, True])
def test_new_operation_recovers_published_original_without_republishing_live_worker(
    preparation, pending_retirement
):
    from judge.container_deployment_config import digest

    value = preparation
    value.run()
    item = value.value.item
    previous = copy.deepcopy(value.record())
    host = value.coordinator()
    token = str(uuid.uuid4())
    with host.locked():
        host.state["current"] = copy.deepcopy(item.generation)
        host.state["operation"].update(id=token, exposure_target=None)
        if pending_retirement:
            host.state["gpu_retirements"] = {
                token + ":" + item.generation["gpu_worker"]: {
                    "schema": 1,
                    "operation": token,
                    "generation": copy.deepcopy(item.generation),
                    "binding_sha256": digest(host.state["gpu_attachment"]["binding"]),
                    "from": copy.deepcopy(host.state["gpu_configuration"]["active"]),
                    "phase": "prepared",
                }
            }
        host.save()
    item.token = token
    before = value.events()
    if pending_retirement:
        with pytest.raises((ValueError, RuntimeError)):
            value.run(recover=True)
        assert value.events() == before
        return
    result = value.run(recover=True)
    assert result["verification"]["passed"]
    assert "publish" not in value.events()[len(before) :]
    assert previous in state(item)["gpu_preparations"].values()
    assert value.record()["operation"] == token
    assert value.record()["attempts"][-1]["startup"] != previous["attempts"][-1]["startup"]
    after = value.events()
    value.run(recover=True)
    # Ensuring the same CPU service is idempotent; no GPU restart, publication,
    # Web replacement or new candidate verification is permitted on this retry.
    assert value.events()[len(after) :] == ["service:worker"]


def test_fresh_candidate_creates_both_fixed_containers_through_normal_lifecycle(preparation):
    value = preparation
    # Only synthetic fixture containers/records exist here. Start this scenario
    # with no pre-created worker; no running installation or real object is reset.
    host = value.coordinator()
    with host.locked():
        for name in ("gpu_runtime", "gpu_workers", "gpu_checks"):
            host.state.pop(name, None)
        host.save()
    value.value.model.change(lambda data: data.update(containers={}))
    value.run()
    assert value.events().count("gpu_worker:create") == 1
    assert value.events().count("gpu_check:create") == 1
    assert value.record()["attempts"][-1]["phase"] == "verified"


@pytest.mark.parametrize("defect", ["route", "pause", "service"])
def test_initial_preparation_requires_original_pause_route_and_service_names(preparation, defect):
    value = preparation
    host = value.coordinator()
    generation = copy.deepcopy(value.value.item.generation)
    if defect == "route":
        value.value.item.model.port = 18000
    elif defect == "pause":
        value.value.item.model.pause = False
    else:
        generation["web"] = "unrecorded-web"
    with host.locked(), pytest.raises((GPUPreparationError, WorkerLifecycleError)):
        host.prepare_candidate(generation, value.value.release)
    assert value.events() == []


def test_failed_preflight_recovers_original_worker_without_erasing_first_attempt(preparation):
    value = preparation
    value.value.model.change(lambda data: data.update(check_code=1))
    with pytest.raises(WorkerLifecycleError):
        value.run()
    old = copy.deepcopy(value.record()["attempts"][0])
    assert old["phase"] == "preflighting" and old["startup"] is None
    value.value.model.change(lambda data: data.update(check_code=0))
    value.run(recover=True)
    assert value.record()["attempts"][0] == old
    assert value.record()["attempts"][-1]["phase"] == "verified"


@pytest.mark.parametrize("stage", ["service:web", "service:worker", "check:cpu"])
def test_interrupted_preparation_retries_original_containers_without_republishing(
    preparation, stage
):
    value = preparation

    def fail(event):
        if event == stage:
            value.hook = lambda event: None
            raise RuntimeError("modeled interrupted preparation")

    value.hook = fail
    with pytest.raises(RuntimeError, match="interrupted"):
        value.run()
    original = copy.deepcopy(value.value.model.read()["containers"])
    value.run()
    assert all(
        value.value.model.read()["containers"][name]["Id"] == info["Id"]
        for name, info in original.items()
    )
    assert value.events().count("publish") == 1


def test_recovery_cleans_original_round_and_verifies_new_start_on_same_database(preparation):
    value = preparation
    value.candidate.failure = "original GPU check lost"
    with pytest.raises(RuntimeError, match="lost"):
        value.run()
    old = copy.deepcopy(value.record())
    old_check = copy.deepcopy(value.candidate.record())
    value.candidate.failure = None
    result = value.run(recover=True)
    record = value.record()
    assert len(record["attempts"]) == 2 and record["attempts"][0] == old["attempts"][0]
    assert record["attempts"][-1]["recovered"] and record["attempts"][-1]["phase"] == "verified"
    assert record["generation"]["database"] == old["generation"]["database"]
    assert result["verification"]["round_id"] != old_check["round_id"]
    assert value.candidate.record(old_check["round_id"])["phase"] == "cleaned"
    assert value.events().count("publish") == 1 and "replace:web" in value.events()
    assert value.run(recover=True)["verification"] == result["verification"]
    assert len(value.record()["attempts"]) == 2


def test_failed_original_cleanup_does_not_begin_another_preparation_attempt(preparation):
    value = preparation
    value.candidate.failure = "still failing"
    with pytest.raises(RuntimeError):
        value.run()
    with pytest.raises(RuntimeError):
        value.run(recover=True)
    assert len(value.record()["attempts"]) == 1
    assert len(state(value.value.item)["gpu_candidate_checks"]) == 1
    value.candidate.failure = None
    value.run(recover=True)
    assert len(value.record()["attempts"]) == 2


def test_verified_preparation_still_requires_current_cpu_and_gpu_readiness(preparation):
    value = preparation
    value.run()
    value.value.model.change(lambda data: data["cpu_services"].update(worker=False))
    with pytest.raises(GPUPreparationError, match="CPU worker"):
        value.run()
    value.value.model.change(lambda data: data["cpu_services"].update(worker=True))
    value.value.snapshot["device"]["state"] = "quarantined"
    with pytest.raises(GPUPreparationError, match="no longer ready"):
        value.run()
    assert len(value.candidate.calls()) == 2


@pytest.mark.parametrize("field", ["device", "runtime_sha256"])
def test_verified_round_must_match_the_actual_device_and_runtime(preparation, field):
    value = preparation
    value.run()
    host = value.coordinator()
    with host.locked():
        row = value.candidate.record()
        record = host.state["gpu_candidate_checks"][record_key(row)]
        record[field] = "GPU-" + str(uuid.uuid4()) if field == "device" else "f" * 64
        record["report"] = expected_report(record)
        record["cleanup_report"] = expected_report(record, True)
        host.save()
    with pytest.raises(GPUPreparationError, match="another startup"):
        value.run()


def test_lost_preparation_confirmation_reuses_already_verified_gpu_round(preparation, monkeypatch):
    value = preparation
    original = deploy.Deployment.check_gpu_candidate

    def bad(host, *args, **kwargs):
        result = original(host, *args, **kwargs)
        return result | {"round_id": str(uuid.uuid4())}

    monkeypatch.setattr(deploy.Deployment, "check_gpu_candidate", bad)
    with pytest.raises(GPUPreparationError, match="missing or ambiguous"):
        value.run()
    assert value.record()["attempts"][-1]["round_id"] is None
    assert value.record()["attempts"][-1]["phase"] == "checking_gpu"
    monkeypatch.setattr(deploy.Deployment, "check_gpu_candidate", original)
    value.run()
    assert len(value.candidate.calls()) == 2


def test_moved_archive_cannot_resume_a_preparation(preparation, tmp_path):
    import shutil

    value = preparation
    value.cpu_failure = True
    with pytest.raises(RuntimeError):
        value.run()
    alternate = tmp_path / "different-archive-location"
    shutil.copytree(value.value.release, alternate)
    before = value.events()
    with pytest.raises(GPUPreparationError, match="identity changed"):
        value.run(release=alternate)
    assert value.events() == before


@pytest.mark.parametrize("field", ["phase", "startup", "round_id", "recovered"])
def test_damaged_preparation_record_fails_closed(preparation, field):
    preparation.run()
    damaged = copy.deepcopy(preparation.record())
    damaged["attempts"][-1][field] = {"unexpected": True}
    with pytest.raises(GPUPreparationError, match="Invalid original"):
        validate_record(damaged)


@pytest.mark.parametrize(
    "phase",
    [
        "publishing",
        "starting_services",
        "resetting_preflight",
        "preflighting",
        "creating",
        "starting",
        "waiting",
        "checking_cpu",
        "checking_gpu",
        "verified",
    ],
)
def test_sigkill_retries_persisted_preparation_without_recreating_gpu_tasks(preparation, phase):
    value = preparation
    pid = os.fork()
    if pid == 0:
        save = deploy.Deployment.save

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            records = host.state.get("gpu_preparations", {}).values()
            if any(row["attempts"][-1]["phase"] == phase for row in records):
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        try:
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
                pytest.fail("Owned candidate subprocess did not reach the intended kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        value.run()
        assert value.record()["attempts"][-1]["phase"] == "verified"
        assert len(value.candidate.calls()) == 2
        assert len(state(value.value.item)["gpu_candidate_checks"]) == 1
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.parametrize("stage", ["recovery_ready", "original_cleaned", "restoring_services"])
def test_sigkill_during_recovery_retains_old_round_and_does_not_restart_again(preparation, stage):
    value = preparation
    value.candidate.failure = "original check failed"
    with pytest.raises(RuntimeError):
        value.run()
    original_round = value.candidate.record()["round_id"]
    value.candidate.failure = None
    value.value.model.guard.close()
    value.value.model.guard = None
    pid = os.fork()
    if pid == 0:
        save = deploy.Deployment.save

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            reached = (
                stage == "recovery_ready"
                and any(
                    row["phase"] == "ready" for row in host.state.get("gpu_recoveries", {}).values()
                )
                or stage == "original_cleaned"
                and any(
                    row["round_id"] == original_round and row["phase"] == "cleaned"
                    for row in host.state.get("gpu_candidate_checks", {}).values()
                )
                or stage == "restoring_services"
                and any(
                    row["attempts"][-1]["phase"] == stage
                    for row in host.state.get("gpu_preparations", {}).values()
                )
            )
            if reached:
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", saved)
                value.run(recover=True)
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
                pytest.fail("Owned recovery subprocess did not reach its kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        value.run(recover=True)
        assert len(value.record()["attempts"]) == 2
        assert value.candidate.record(original_round)["phase"] == "cleaned"
        assert value.events().count("gpu_worker:start") == 2
        assert len(value.candidate.calls()) == 4
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
