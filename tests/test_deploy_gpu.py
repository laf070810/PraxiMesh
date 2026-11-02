"""Real private files/locks and modeled daemon identity; never GPU readiness."""

import copy
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge.container_controller import manifest
from judge.container_journal import ContainerJournal
from judge.container_service import LocalEngine
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from judge.sandbox import SandboxUnavailable
from scripts import deploy, deploy_gpu
from tests.test_deploy import Crash
from tests.test_deploy import private_config as private_config


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def synthetic_observer():
    """Static metadata fixture; no device is queried, selected, or authorized."""
    return {
        "schema": 2,
        "target": {
            "index": 0,
            "uuid": "GPU-" + str(uuid.uuid4()),
            "model": "synthetic-unused-device",
            "driver": "590.48.01",
            "architecture": [8, 6],
        },
        "minor": 0,
        "library": {
            "schema": 2,
            "profile": "nvidia-inventory-v2",
            "driver_version": "590.48.01",
            "library": {
                "name": "libnvidia-ml.so.590.48.01",
                "soname": "libnvidia-ml.so.1",
                "size": 1,
                "sha256": "a" * 64,
            },
            "utility": {"name": "nvidia-smi", "size": 1, "sha256": "b" * 64},
        },
    }


@pytest.fixture
def prepared(tmp_path, monkeypatch, private_config):
    socket_dir = tmp_path / "engine"
    socket_dir.mkdir(mode=0o700)
    connection = socket.socket(socket.AF_UNIX)
    connection.bind(str(socket_dir / "docker.sock"))
    engine = LocalEngine(socket_dir / "docker.sock")
    engine_id = str(uuid.uuid4())
    calls = []

    def run(*args, **kwargs):
        assert args == ("info", "--format", "{{.ID}}")
        calls.append(args)
        return SimpleNamespace(stdout=engine_id.encode(), returncode=0)

    monkeypatch.setattr(engine, "verify", lambda: {"ID": engine_id})
    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(deploy_gpu, "LocalEngine", lambda path: engine)
    observer = synthetic_observer()
    root = tmp_path / "gpu"
    root.mkdir(mode=0o700)
    (root / "locks").mkdir(mode=0o700)
    (root / "journal").mkdir(mode=0o700)
    device = observer["target"]["uuid"]
    with DeviceLock(root / "locks", device) as guard:
        with ContainerJournal(root / "journal", guard, engine):
            write_private(
                root / "mounts.json",
                manifest(
                    root,
                    engine,
                    host_processes=True,
                    device_observer=observer,
                ),
            )
    directory = tmp_path / "site"
    directory.mkdir(mode=0o700)
    path, original = private_config
    caddy = tmp_path / "caddy"
    caddy.write_text("#!/bin/sh\nexit 99\n")
    caddy.chmod(0o700)
    original["proxy"]["caddy"] = str(caddy)
    initial = deploy.Deployment(original, directory)
    initial.state = {
        "format": 1,
        "site_id": uuid.uuid4().hex,
        "instance": original["instance"],
        "config_digest": initial.config_digest,
        "current": None,
        "operation": None,
    }
    initial.save()
    config = copy.deepcopy(original)
    config["gpu"] = {"state": str(root), "docker_socket": str(engine.path), "device": device}
    write_private(path, config)
    config = deploy.configuration(path)

    def coordinator():
        result = deploy.Deployment(config, directory)
        result.run = lambda *args, **kwargs: run(*args, **kwargs).stdout
        return result

    item = SimpleNamespace(
        root=root,
        device=device,
        config=config,
        config_path=path,
        directory=directory,
        engine=engine,
        engine_id=engine_id,
        calls=calls,
        coordinator=coordinator,
    )
    try:
        yield item
    finally:
        connection.close()


def attach(prepared):
    instance = prepared.coordinator()
    with instance.locked(attach_gpu=True):
        instance.attach_gpu()
    return instance


def state(prepared):
    return json.loads((prepared.directory / "deployment.json").read_text())


def test_only_explicit_attachment_accepts_adding_gpu_config(prepared):
    with pytest.raises(deploy.DeploymentError, match="Configuration changed"):
        with prepared.coordinator().locked():
            pass
    lock = prepared.root / "locks" / (prepared.device + ".lock")
    original_lock = lock.stat().st_ino
    original_mounts = (prepared.root / "mounts.json").read_bytes()
    instance = attach(prepared)
    attachment = state(prepared)["gpu_attachment"]
    owner = prepared.root / deploy_gpu.OWNER
    assert attachment["phase"] == "attached"
    assert json.loads(owner.read_text()) == attachment["binding"]
    assert state(prepared)["config_digest"] == instance.config_digest
    assert lock.stat().st_ino == original_lock
    assert (prepared.root / "mounts.json").read_bytes() == original_mounts
    assert not (prepared.root / "worker.json").exists()
    assert instance._database is None
    before = (owner.stat().st_ino, owner.stat().st_mtime_ns, instance.path.read_bytes())
    attach(prepared)
    assert before == (owner.stat().st_ino, owner.stat().st_mtime_ns, instance.path.read_bytes())
    with prepared.coordinator().locked():
        pass


@pytest.mark.parametrize("field", ["application", "database", "networks", "gpu"])
def test_attachment_cannot_also_change_unrelated_or_bound_configuration(prepared, field):
    if field == "gpu":
        attach(prepared)
        prepared.config["gpu"]["device"] = "GPU-" + str(uuid.uuid4())
    elif field == "application":
        prepared.config[field]["secret_key"] += "different"
    elif field == "database":
        prepared.config[field]["worker_password"] += "different"
    else:
        prepared.config[field]["backend"] = "other-network"
    with pytest.raises(deploy.DeploymentError, match="Configuration changed"):
        attach(prepared)


@pytest.mark.parametrize(
    "defect", ["index", "uppercase", "relative", "traversal", "extra", "symlink"]
)
def test_gpu_configuration_rejects_ambiguous_node_selection(prepared, defect):
    value = copy.deepcopy(prepared.config)
    if defect == "index":
        value["gpu"]["device"] = "0"
    elif defect == "uppercase":
        value["gpu"]["device"] = "GPU-AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    elif defect == "relative":
        value["gpu"]["state"] = "var/gpu"
    elif defect == "traversal":
        value["gpu"]["state"] += "/../gpu"
    elif defect == "extra":
        value["gpu"]["skip_startup"] = True
    else:
        link = prepared.root.with_name("linked-state")
        link.symlink_to(prepared.root)
        value["gpu"]["state"] = str(link)
    write_private(prepared.config_path, value)
    with pytest.raises(deploy.DeploymentError, match="GPU node"):
        deploy.configuration(prepared.config_path)


@pytest.mark.parametrize(
    "name",
    [
        "worker.json",
        "receipts",
        "journal/unknown.active.json",
        "journal/old.archive.json",
        "journal/local-practice.json",
        "journal/local-operation.json",
        "locks/another.lock",
        deploy_gpu.OWNER,
    ],
)
def test_first_attachment_does_not_adopt_or_delete_existing_execution_state(prepared, name):
    path = prepared.root / name
    write_private(path, {"existing": "untouched"})
    original = path.read_bytes()
    previous = (prepared.directory / "deployment.json").read_bytes()
    with pytest.raises(ValueError, match="unused prepared state"):
        attach(prepared)
    assert path.read_bytes() == original
    assert (prepared.directory / "deployment.json").read_bytes() == previous


def test_original_device_lock_excludes_coordinator_without_changing_state(prepared):
    with DeviceLock(prepared.root / "locks", prepared.device):
        with pytest.raises(DeviceLockError, match="Another controller"):
            attach(prepared)
    assert "gpu_attachment" not in state(prepared)
    assert not (prepared.root / deploy_gpu.OWNER).exists()


@pytest.mark.parametrize("point", ["intent", "owner", "confirmation"])
def test_interrupted_attachment_resumes_only_its_durable_intent(prepared, monkeypatch, point):
    instance = prepared.coordinator()
    original_save = instance.save
    original_publish = deploy_gpu.GPUDeploymentNode.publish_owner

    def save(*args, **kwargs):
        original_save(*args, **kwargs)
        phase = instance.state["gpu_attachment"]["phase"]
        if (
            point == "intent"
            and phase == "prepared"
            or point == "confirmation"
            and phase == "attached"
        ):
            raise Crash()

    def publish(node, binding):
        original_publish(node, binding)
        if point == "owner":
            raise Crash()

    instance.save = save
    with monkeypatch.context() as patch:
        patch.setattr(deploy_gpu.GPUDeploymentNode, "publish_owner", publish)
        with pytest.raises(Crash):
            with instance.locked(attach_gpu=True):
                instance.attach_gpu()
    binding = state(prepared)["gpu_attachment"]["binding"]
    attach(prepared)
    assert state(prepared)["gpu_attachment"] == {"phase": "attached", "binding": binding}


def prepare_interruption(prepared):
    instance = prepared.coordinator()
    original_save = instance.save

    def save():
        original_save()
        raise Crash()

    instance.save = save
    with pytest.raises(Crash):
        with instance.locked(attach_gpu=True):
            instance.attach_gpu()
    return state(prepared)["gpu_attachment"]["binding"]


@pytest.mark.parametrize("kind", ["partial", "unknown", "hardlinked"])
def test_resume_only_cleans_its_exact_private_temporary(prepared, kind):
    binding = prepare_interruption(prepared)
    name = deploy_gpu.GPUDeploymentNode.temporary(binding)
    if kind == "unknown":
        name += ".other"
    path = prepared.root / name
    path.write_bytes(b'{"interrupted":')
    path.chmod(0o600)
    if kind == "hardlinked":
        os.link(path, prepared.root.parent / "foreign-link")
    if kind == "partial":
        attach(prepared)
        assert not path.exists()
    else:
        with pytest.raises((ValueError, SandboxUnavailable)):
            attach(prepared)
        assert path.read_bytes() == b'{"interrupted":'


@pytest.mark.parametrize("action", ["apply", "recover", "maintain"])
def test_pending_attachment_blocks_deployment_mutations(prepared, action):
    prepare_interruption(prepared)
    instance = prepared.coordinator()
    with instance.locked(attach_gpu=True):
        with pytest.raises(deploy.DeploymentError, match="attach-gpu"):
            if action == "apply":
                instance.apply(Path("unused"), "unused")
            else:
                getattr(instance, action)()
    assert instance._database is None


def test_cli_rejects_pending_attachment_before_connecting_to_postgres(prepared, monkeypatch):
    prepare_interruption(prepared)
    # Use the original CPU config to reach the pending intent under the normal lock.
    original = {key: value for key, value in prepared.config.items() if key != "gpu"}
    instance = deploy.Deployment(original, prepared.directory)
    instance._database = SimpleNamespace(operation_lock=lambda: pytest.fail("Database access"))
    monkeypatch.setattr(deploy, "configuration", lambda *a: original)
    monkeypatch.setattr(deploy, "Deployment", lambda *a: instance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deploy.py",
            "--config",
            "unused",
            "--state",
            str(prepared.directory),
            "maintain",
        ],
    )
    with pytest.raises(SystemExit) as exit_code:
        deploy.main()
    assert exit_code.value.code == 1


@pytest.mark.parametrize(
    "defect", ["owner", "owner-bool", "mounts", "lock", "missing-owner", "engine"]
)
def test_bound_state_cannot_be_reinterpreted_on_retry(prepared, defect):
    attach(prepared)
    owner = prepared.root / deploy_gpu.OWNER
    if defect in {"owner", "owner-bool"}:
        data = json.loads(owner.read_text())
        data["site_id" if defect == "owner" else "schema"] = (
            uuid.uuid4().hex if defect == "owner" else True
        )
        write_private(owner, data)
    elif defect == "mounts":
        path = prepared.root / "mounts.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif defect == "lock":
        path = prepared.root / "locks" / (prepared.device + ".lock")
        path.rename(path.with_suffix(".old"))
        path.touch(mode=0o600)
    elif defect == "missing-owner":
        owner.unlink()
    else:
        prepared.coordinator = lambda: altered_engine_coordinator(prepared)
    before = (prepared.directory / "deployment.json").read_bytes()
    with pytest.raises(ValueError):
        attach(prepared)
    assert (prepared.directory / "deployment.json").read_bytes() == before


def altered_engine_coordinator(prepared):
    instance = deploy.Deployment(prepared.config, prepared.directory)
    instance.run = lambda *args, **kwargs: str(uuid.uuid4()).encode()
    return instance


def test_copied_coordinator_cannot_claim_original_gpu_owner(prepared):
    attach(prepared)
    duplicate = prepared.directory.with_name("copied-site")
    duplicate.mkdir(mode=0o700)
    (duplicate / "deployment.json").write_bytes(
        (prepared.directory / "deployment.json").read_bytes()
    )
    (duplicate / "deployment.json").chmod(0o600)
    instance = deploy.Deployment(prepared.config, duplicate)
    instance.run = lambda *args, **kwargs: prepared.engine_id.encode()
    with pytest.raises(ValueError, match="different original state"):
        with instance.locked(attach_gpu=True):
            instance.attach_gpu()


@pytest.mark.parametrize(
    "record", ["mounts.json", "journal/installation.json", "journal/publication.lock"]
)
@pytest.mark.parametrize("defect", ["missing", "permissions", "symlink"])
def test_attachment_requires_original_private_records(prepared, record, defect):
    path = prepared.root / record
    if defect == "permissions":
        path.chmod(0o644)
    else:
        path.rename(path.with_name(path.name + ".original"))
        if defect == "symlink":
            path.symlink_to(path.with_name(path.name + ".original"))
    with pytest.raises((OSError, ValueError, SandboxUnavailable)):
        attach(prepared)
    assert "gpu_attachment" not in state(prepared)
