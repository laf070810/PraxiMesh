"""Real private files/source archives; explicit Docker/DB models, never GPU proof."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge.container_controller import CONTROLLER_SOURCES
from judge.container_deployment_config import digest
from judge.container_gpu import SOURCES as TASK_SOURCES
from judge.container_gpu import image_contract
from scripts import deploy_gpu_readiness as module
from scripts.deploy_gpu_readiness import GPUReadiness, GPUReadinessError, archived_artifacts
from scripts.deploy_gpu_worker import WorkerLifecycleError
from scripts.release import ReleaseError, source_archive
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
def readiness(lifecycle, configured, monkeypatch, tmp_path):
    item = lifecycle.item
    item.generation = copy.deepcopy(item.generation)
    item.generation.update(
        id="123456abcdef",
        database="px_example_123456abcdef",
        gpu_worker="px-example-123456abcdef-gpu-worker",
        release_sha256="a" * 64,
    )
    root = Path(__file__).resolve().parents[1]
    contents = {name: (root / name).read_bytes() for name in CONTROLLER_SOURCES}
    manifest = item.generation["manifest"]
    manifest["judge"] = {"bundle_sha256": "a" * 64, "policy_sha256": "b" * 64}
    manifest["source_files"] = {n: hashlib.sha256(v).hexdigest() for n, v in contents.items()}
    manifest["gpu"]["source_scopes"].update(controller=list(contents), task=list(TASK_SOURCES))
    environment = copy.deepcopy(configured.adapter.environment)
    manifest["gpu"]["contracts"].update(
        execution=environment["execution_digest"],
        controller=environment["artifacts"]["controller_contract"],
        task=image_contract(manifest["gpu"]["driver"]),
    )
    lifecycle.model.image_config["Labels"]["io.praximesh.controller.contract"] = manifest["gpu"][
        "contracts"
    ]["controller"]
    remember(item, item.generation)
    item.publish(item.generation, enabled=True)
    release = tmp_path / "frozen-release"
    release.mkdir()
    source_archive(root, manifest["source_files"], release / "source.tar.gz")
    # Full image archive verification is tested separately; source reads below are real.
    monkeypatch.setattr(module, "verify", lambda path, sha: copy.deepcopy(manifest))
    engine = dict(configured.engine, ID=item.bound.prepared.engine_id)
    monkeypatch.setattr(item.bound.prepared.engine, "verify", lambda: copy.deepcopy(engine))
    lifecycle.act("check", "check")
    lifecycle.act("create")
    info = lifecycle.act("start")
    observed = datetime.fromisoformat(
        info["State"]["StartedAt"].replace("Z", "+00:00")
    ) + timedelta(seconds=1)
    observer = json.loads((item.root / "mounts.json").read_text())["device_observer"]
    environment["target"] = observer["target"]
    environment["device_spec"].update(uuid=observer["target"]["uuid"], minor=observer["minor"])
    environment["artifacts"] = copy.deepcopy(archived_artifacts(manifest, contents)) | {
        "engine": module.engine_identity(engine)
    }
    snapshot = {
        "observed_at": observed,
        "pending": False,
        "reservations": False,
        "device": {
            "id": item.bound.prepared.device,
            "state": "ready",
            "worker_id": item.generation["gpu_worker"],
            "owner_token": uuid.uuid4(),
            "fingerprint": digest(environment),
            "environment": environment,
            "checked_at": observed,
            "job_id": None,
            "reservation_token": None,
        },
    }
    calls = []

    def read(name, token, device):
        assert (name, token, device) == (
            item.generation["database"],
            item.token,
            item.bound.prepared.device,
        )
        calls.append((name, token, device))
        return copy.deepcopy(snapshot)

    monkeypatch.setattr(item.database, "gpu_worker_snapshot", read)

    def observe(wait=False):
        host = item.coordinator()
        with host.locked():
            return host.gpu_worker_ready(item.generation, release, wait=wait)

    return SimpleNamespace(
        item=item,
        model=lifecycle.model,
        act=lifecycle.act,
        snapshot=snapshot,
        observe=observe,
        calls=calls,
        contents=contents,
        release=release,
        engine=engine,
        started=info["State"]["StartedAt"],
    )


def test_readiness_observes_original_full_environment_without_mutating_state(readiness):
    value = readiness
    before = state(value.item)
    calls = len(value.model.read()["calls"])
    result = value.observe()
    assert result["started_at"] == value.started
    assert result["fingerprint"] == value.snapshot["device"]["fingerprint"]
    assert state(value.item) == before
    assert not (value.item.root / "receipts").exists()
    assert all(call[1] in {"ls", "inspect"} for call in value.model.read()["calls"][calls:])
    assert value.model.guard is not None  # Observer never takes the worker's device lock.
    assert value.item.database.config["worker_password"] not in json.dumps(result)


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "quarantined",
        "reserved",
        "other-worker",
        "other-device",
        "old-start",
        "stale",
        "future",
        "no-heartbeat",
        "naive-heartbeat",
        "pending",
        "orphan-any-device",
        "orphan-job",
        "orphan-reservation",
    ],
)
def test_incomplete_or_unavailable_startup_never_counts_as_ready(readiness, defect):
    value, row = readiness.snapshot, readiness.snapshot["device"]
    if defect == "missing":
        value["device"] = None
    elif defect in {"quarantined", "reserved"}:
        row["state"] = defect
    elif defect == "other-worker":
        row["worker_id"] = "previous-worker"
    elif defect == "other-device":
        row["id"] = "GPU-" + str(uuid.uuid4())
    elif defect in {"old-start", "stale", "future"}:
        if defect == "stale":
            value["observed_at"] += timedelta(seconds=60)
            row["checked_at"] = value["observed_at"]
        row["checked_at"] += timedelta(seconds={"old-start": -2, "stale": -30, "future": 1}[defect])
    elif defect == "no-heartbeat":
        row["checked_at"] = None
    elif defect == "naive-heartbeat":
        row["checked_at"] = row["checked_at"].replace(tzinfo=None)
    elif defect == "pending":
        value["pending"] = True
    elif defect == "orphan-any-device":
        value["reservations"] = True
    elif defect == "orphan-job":
        row["job_id"] = uuid.uuid4()
    else:
        row["reservation_token"] = uuid.uuid4()
    assert readiness.observe() is None


@pytest.mark.parametrize(
    "field",
    [
        "runtime_contract",
        "task_contract",
        "controller_contract",
        "task_image",
        "runsc_sha256",
        "runsc_release",
        "nvrtc_archive_sha256",
        "nvrtc_libraries",
        "docker_cli_version",
        "docker_cli_sha256",
        "engine",
        "driver_manifest",
        "management_manifest",
    ],
)
def test_valid_but_wrong_artifact_identity_is_not_accepted(readiness, field):
    row = readiness.snapshot["device"]
    artifacts = row["environment"]["artifacts"]
    if field in {"driver_manifest", "management_manifest"}:
        if field == "driver_manifest":
            next(iter(artifacts[field]["libraries"].values()))["sha256"] = "e" * 64
        else:
            artifacts[field]["utility"]["sha256"] = "e" * 64
    elif field == "engine":
        artifacts[field]["version"] = "29.9.3"
    elif field == "nvrtc_libraries":
        artifacts[field][next(iter(artifacts[field]))] = "e" * 64
    elif field == "runsc_release":
        artifacts[field] = "release-20261027.0"
    elif field == "docker_cli_version":
        artifacts[field] = "29.9.3"
    else:
        artifacts[field] = ("sha256:" if field == "task_image" else "") + "e" * 64
    row["fingerprint"] = digest(row["environment"])
    with pytest.raises(GPUReadinessError):
        readiness.observe()


@pytest.mark.parametrize("defect", ["execution", "target", "minor", "fingerprint", "owner"])
def test_original_policy_device_and_owner_are_required(readiness, defect):
    row = readiness.snapshot["device"]
    if defect == "execution":
        row["environment"]["execution_digest"] = "e" * 64
    elif defect == "target":
        row["environment"]["target"]["model"] = "other-model"
    elif defect == "minor":
        row["environment"]["device_spec"]["minor"] += 1
    elif defect == "owner":
        row["owner_token"] = "bad-owner"
    row["fingerprint"] = "e" * 64 if defect == "fingerprint" else digest(row["environment"])
    with pytest.raises(GPUReadinessError):
        readiness.observe()


def test_previous_success_is_not_cached(readiness):
    assert readiness.observe()
    readiness.snapshot["device"]["state"] = "quarantined"
    assert readiness.observe() is None
    assert len(readiness.calls) == 3


@pytest.mark.parametrize("defect", ["stop", "restart", "pause", "maintenance"])
def test_changed_instance_or_operation_after_database_read_is_refused(
    readiness, monkeypatch, defect
):
    original = readiness.item.database.gpu_worker_snapshot

    def changed(*args):
        result = original(*args)
        if defect == "pause":
            readiness.item.model.pause = False
        elif defect == "maintenance":
            readiness.item.model.port = 1234
        else:

            def change(value):
                row = value["containers"]["/" + readiness.item.generation["gpu_worker"]]["State"]
                row.update(
                    Running=False, Pid=0, Status="exited"
                ) if defect == "stop" else row.update(StartedAt="2026-11-01T12:01:00Z")

            readiness.model.change(change)
        return result

    monkeypatch.setattr(readiness.item.database, "gpu_worker_snapshot", changed)
    with pytest.raises((GPUReadinessError, WorkerLifecycleError)):
        readiness.observe()


def test_exit_during_environment_validation_is_not_accepted(readiness, monkeypatch):
    original = GPUReadiness.environment

    def changed(check, *args):
        original(check, *args)
        readiness.model.change(
            lambda value: value["containers"]["/" + readiness.item.generation["gpu_worker"]][
                "State"
            ].update(Running=False, Pid=0, Status="exited")
        )

    monkeypatch.setattr(GPUReadiness, "environment", changed)
    with pytest.raises(GPUReadinessError, match="not running"):
        readiness.observe()


@pytest.mark.parametrize("defect", ["owner", "environment", "quarantine", "reservation", "pending"])
def test_final_snapshot_rechecks_owner_environment_and_all_work(readiness, monkeypatch, defect):
    original = readiness.item.database.gpu_worker_snapshot

    def changed(*args):
        result = original(*args)
        if len(readiness.calls) == 2:
            row = result["device"]
            if defect == "owner":
                row["owner_token"] = uuid.uuid4()
            elif defect == "environment":
                row["environment"]["artifacts"]["docker_cli_sha256"] = "e" * 64
                row["fingerprint"] = digest(row["environment"])
            elif defect == "quarantine":
                row["state"] = "quarantined"
            else:
                result["reservations" if defect == "reservation" else "pending"] = True
        return result

    monkeypatch.setattr(readiness.item.database, "gpu_worker_snapshot", changed)
    if defect in {"owner", "environment"}:
        with pytest.raises(GPUReadinessError, match="owner or environment changed"):
            readiness.observe()
    else:
        assert readiness.observe() is None


def clock(monkeypatch, wall):
    value = SimpleNamespace(elapsed=0.0, sleeps=[])

    def sleep(seconds):
        value.sleeps.append(seconds)
        value.elapsed += seconds

    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            time=lambda: wall + value.elapsed,
            monotonic=lambda: value.elapsed,
            sleep=sleep,
        ),
    )
    return value


def test_expiry_during_environment_validation_is_not_accepted(readiness, monkeypatch):
    timer = clock(monkeypatch, readiness.snapshot["observed_at"].timestamp())
    original = GPUReadiness.environment

    def changed(check, *args):
        original(check, *args)
        timer.elapsed += module.FRESH_SECONDS

    monkeypatch.setattr(GPUReadiness, "environment", changed)
    assert readiness.observe() is None


def test_wait_keeps_original_startup_budget_and_does_not_restart(readiness, monkeypatch):
    started = datetime.fromisoformat(readiness.started.replace("Z", "+00:00")).timestamp()
    timer = clock(monkeypatch, started + module.STARTUP_SECONDS - 1)
    readiness.snapshot["device"]["state"] = "quarantined"
    before = state(readiness.item)
    with pytest.raises(GPUReadinessError, match="timed out"):
        readiness.observe(wait=True)
    assert timer.elapsed == 1 and timer.sleeps == [0.5, 0.5]
    with pytest.raises(GPUReadinessError, match="timed out"):
        readiness.observe(wait=True)
    assert timer.elapsed == 1 and state(readiness.item) == before
    assert readiness.model.guard is not None


def test_original_worker_can_become_ready_during_wait(readiness, monkeypatch):
    timer = clock(monkeypatch, readiness.snapshot["observed_at"].timestamp())
    original = readiness.item.database.gpu_worker_snapshot

    def pending(*args):
        result = original(*args)
        if len(readiness.calls) == 1:
            result["device"]["state"] = "quarantined"
        return result

    monkeypatch.setattr(readiness.item.database, "gpu_worker_snapshot", pending)
    assert readiness.observe(wait=True)["started_at"] == readiness.started
    assert timer.sleeps == [0.5]


def test_source_archive_tamper_cannot_borrow_the_working_tree(readiness):
    path = readiness.release / "source.tar.gz"
    with path.open("ab") as stream:
        stream.write(b"changed archive")
    with pytest.raises((OSError, ReleaseError)):
        readiness.observe()


@pytest.mark.parametrize("defect", ["call", "duplicate", "missing-source", "wrong-task"])
def test_frozen_declarations_are_data_and_require_complete_sources(readiness, defect, tmp_path):
    contents = dict(readiness.contents)
    manifest = copy.deepcopy(readiness.item.generation["manifest"])
    path = "judge/container_runtime_service.py"
    canary = tmp_path / "not-executed"
    if defect == "call":
        contents[path] = (
            f"from pathlib import Path\nSOURCES = Path({str(canary)!r}).write_text('bad')\n".encode()
        )
    elif defect == "duplicate":
        contents[path] += b"\nSOURCES = ()\n"
    elif defect == "missing-source":
        del contents["judge/container_oci.py"]
    else:
        manifest["gpu"]["contracts"]["task"] = "e" * 64
    with pytest.raises(GPUReadinessError):
        archived_artifacts(manifest, contents)
    assert not canary.exists()
