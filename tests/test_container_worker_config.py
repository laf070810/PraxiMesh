"""Configuration boundaries only; synthetic identities never authorize a GPU."""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from judge.container_worker_config import database_settings, read_config, validate_config
from judge.container_worker_image import source_files, worker_digest
from judge.sandbox import SandboxUnavailable
from scripts import configure_container_worker as preparation
from scripts.configure_container_worker import configure
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle


@pytest.fixture
def configuration(configured):
    return {
        "schema": 1,
        "enabled": False,
        "gpu": configured.adapter.target["uuid"],
        "worker_id": "private-worker",
        "task_image": configured.adapter.image,
        "driver_manifest": copy.deepcopy(configured.adapter.driver_manifest),
        "database": {"url": "postgresql://worker:p%40ss@db:5432/private_db", "sslmode": "disable"},
    }


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--prepare-only"],
        ["--prepare-only", "--initialize", "--enable"],
        ["--prepare-only", "--initialize", "--worker-id", "unexpected"],
        ["--prepare-only", "--initialize", "--database-file", "unused.json"],
        ["--prepare-only", "--initialize"],
        ["--initialize"],
        [],
    ],
)
def test_invalid_preparation_options_do_not_read_a_device(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(preparation, "initialize_state", lambda *a: pytest.fail("Device read"))
    monkeypatch.setattr(preparation, "LocalEngine", lambda *a: pytest.fail("Engine access"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_container_worker.py",
            "--state",
            str(tmp_path / "new"),
            *arguments,
        ],
    )
    assert preparation.main() == 1
    assert not (tmp_path / "new").exists()


def test_prepare_only_delegates_explicit_metadata_initialization_without_worker_settings(
    tmp_path,
    monkeypatch,
    configuration,
    configured,
):
    metadata = tmp_path / "worker-image.json"
    write_private(
        metadata,
        {
            "schema": 1,
            "image": "sha256:" + "c" * 64,
            "management_manifest": configured.adapter.environment["artifacts"][
                "management_manifest"
            ],
        },
    )
    engine = object()
    calls = []
    monkeypatch.setattr(preparation, "LocalEngine", lambda *a: engine)
    monkeypatch.setattr(preparation, "initialize_state", lambda *args: calls.append(args))
    monkeypatch.setattr(preparation, "configure", lambda *a, **kw: pytest.fail("Worker settings"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_container_worker.py",
            "--state",
            str(tmp_path / "new"),
            "--initialize",
            "--prepare-only",
            "--gpu",
            configuration["gpu"],
            "--docker-socket",
            str(tmp_path / "docker.sock"),
            "--worker-image-manifest",
            str(metadata),
        ],
    )
    assert preparation.main() == 0
    assert len(calls) == 1 and calls[0][1] is engine and calls[0][2] == configuration["gpu"]
    assert calls[0][3] == json.loads(metadata.read_text())["management_manifest"]
    assert not (tmp_path / "new" / "worker.json").exists()


def test_malformed_database_is_rejected_before_new_installation_device_read(
    tmp_path,
    monkeypatch,
    configuration,
):
    driver = tmp_path / "driver.json"
    database = tmp_path / "database.json"
    metadata = tmp_path / "worker-image.json"
    write_private(driver, configuration["driver_manifest"])
    write_private(
        database, {"url": "postgresql://worker@db/without_password", "sslmode": "disable"}
    )
    write_private(metadata, {"schema": 1, "image": "sha256:" + "c" * 64})
    monkeypatch.setattr(preparation, "initialize_state", lambda *a: pytest.fail("Device read"))
    monkeypatch.setattr(preparation, "LocalEngine", lambda *a: pytest.fail("Engine access"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_container_worker.py",
            "--state",
            str(tmp_path / "new"),
            "--initialize",
            "--gpu",
            configuration["gpu"],
            "--docker-socket",
            str(tmp_path / "docker.sock"),
            "--worker-image-manifest",
            str(metadata),
            "--worker-id",
            "test",
            "--task-image",
            configuration["task_image"],
            "--driver-manifest",
            str(driver),
            "--database-file",
            str(database),
        ],
    )
    assert preparation.main() == 1


@pytest.mark.parametrize(
    "defect",
    [
        "schema",
        "enabled",
        "gpu-index",
        "worker",
        "image-tag",
        "driver",
        "extra",
        "url-query",
        "url-fragment",
        "url-password",
        "url-database",
        "url-port",
        "url-newline",
        "url-sqlite",
        "url-host",
        "sslmode",
    ],
)
def test_invalid_configuration_is_rejected(configuration, defect):
    value = copy.deepcopy(configuration)
    if defect == "schema":
        value["schema"] = True
    elif defect == "enabled":
        value["enabled"] = "true"
    elif defect == "gpu-index":
        value["gpu"] = "0"
    elif defect == "worker":
        value["worker_id"] = "worker\nsecret"
    elif defect == "image-tag":
        value["task_image"] = "gpu:latest"
    elif defect == "driver":
        value["driver_manifest"]["libraries"] = {}
    elif defect == "extra":
        value["skip_startup"] = True
    elif defect == "sslmode":
        value["database"]["sslmode"] = "prefer"
    else:
        value["database"]["url"] = {
            "url-query": "postgresql://worker:p@db/test?options=-crole=admin",
            "url-fragment": "postgresql://worker:p@db/test#secret",
            "url-password": "postgresql://worker@db/test",
            "url-database": "postgresql://worker:p@db/",
            "url-port": "postgresql://worker:p@db:0/test",
            "url-newline": "postgresql://worker:p@db/test\n",
            "url-sqlite": "sqlite:///tmp/test.db",
            "url-host": "postgresql:///test",
        }[defect]
    with pytest.raises((ValueError, TypeError)):
        validate_config(value)


@pytest.mark.parametrize(
    "defect", ["symlink", "hardlink", "mode", "directory", "oversized", "duplicate"]
)
def test_private_config_rejects_links_modes_size_and_duplicate_fields(
    tmp_path, configuration, defect
):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "worker.json"
    write_private(path, configuration)
    if defect == "symlink":
        path.rename(root / "target")
        path.symlink_to(root / "target")
    elif defect == "hardlink":
        os.link(path, root / "linked")
    elif defect == "mode":
        path.chmod(0o644)
    elif defect == "directory":
        root.chmod(0o755)
    elif defect == "oversized":
        path.write_text(" " * (32 * 1024 + 1))
    elif defect == "duplicate":
        path.write_text('{"schema": 1,' + json.dumps(configuration)[1:])
    with pytest.raises((ValueError, OSError, SandboxUnavailable)):
        read_config(root)


def test_private_config_parses_encoded_credentials_and_bounded_connections(tmp_path, configuration):
    write_private(tmp_path / "worker.json", configuration)
    loaded = read_config(tmp_path)
    assert loaded == configuration
    database = database_settings(loaded)
    assert database["PASSWORD"] == "p@ss"
    assert database["OPTIONS"] == {"sslmode": "disable", "connect_timeout": 5}


def test_verify_full_requires_private_ca_file(tmp_path, configuration):
    configuration["database"]["sslmode"] = "verify-full"
    write_private(tmp_path / "worker.json", configuration)
    with pytest.raises(FileNotFoundError):
        read_config(tmp_path)
    ca = tmp_path / "database-ca.pem"
    ca.write_text("Synthetic CA placeholder; no connection attempted.")
    ca.chmod(0o644)
    with pytest.raises(SandboxUnavailable):
        read_config(tmp_path)
    ca.chmod(0o600)
    assert read_config(tmp_path) == configuration


def test_configuration_uses_original_lock_and_never_overwrites_existing_state(
    tmp_path,
    configured,
    configuration,
):
    from judge.gpu_device_lock import DeviceLock, DeviceLockError

    (tmp_path / "locks").mkdir(mode=0o700)
    observer = copy.deepcopy(configured.control.saved["device_observer"])
    observer.update(schema=2, minor=0)
    write_private(tmp_path / "mounts.json", {"version": 4, "device_observer": observer})
    args = (
        tmp_path,
        configuration["worker_id"],
        configuration["task_image"],
        configuration["driver_manifest"],
        configuration["database"],
    )
    with DeviceLock(tmp_path / "locks", configuration["gpu"]):
        with pytest.raises(DeviceLockError):
            configure(*args)
    assert not (tmp_path / "worker.json").exists()
    configure(*args)
    assert read_config(tmp_path) == configuration
    before = (tmp_path / "worker.json").read_bytes()
    with pytest.raises(FileExistsError):
        configure(*args, enabled=True)
    assert (tmp_path / "worker.json").read_bytes() == before
    assert not (tmp_path / "receipts").exists()


def test_standalone_configuration_cannot_write_coordinator_owned_state(
    tmp_path,
    configured,
    configuration,
):
    (tmp_path / "locks").mkdir(mode=0o700)
    observer = copy.deepcopy(configured.control.saved["device_observer"])
    observer.update(schema=2, minor=0)
    write_private(tmp_path / "mounts.json", {"version": 4, "device_observer": observer})
    write_private(tmp_path / preparation.OWNER, {"synthetic": "already assigned"})
    with pytest.raises(ValueError, match="deployment coordinator"):
        configure(
            tmp_path,
            configuration["worker_id"],
            configuration["task_image"],
            configuration["driver_manifest"],
            configuration["database"],
        )
    assert not (tmp_path / "worker.json").exists()


def test_worker_image_scope_includes_application_entry_and_every_controller_source():
    from judge.container_controller import CONTROLLER_SOURCES

    files = set(source_files())
    assert set(CONTROLLER_SOURCES) <= files
    assert {
        "praximesh/settings.py",
        "core/models.py",
        "core/management/commands/container_gpu_worker.py",
        "scripts/container_worker_entry.py",
        "requirements.lock",
        "deploy/compose.gpu-worker.yaml",
    } <= files
    assert len(worker_digest()) == 64
    assert all(Path(name).suffix != ".pyc" and "__pycache__" not in name for name in files)


@pytest.mark.parametrize("occupied", [False, True])
def test_explicit_new_state_uses_original_mount_and_lock_identities(
    tmp_path,
    configured,
    monkeypatch,
    occupied,
):
    from scripts import configure_container_worker as module
    from tests.test_container_journal import Engine

    target = configured.adapter.target
    root = tmp_path / "new-state"
    monkeypatch.setattr(module, "identify", lambda device: copy.deepcopy(target))
    processes = "<process_info><pid>123</pid></process_info>" if occupied else ""
    xml = (
        f"<nvidia_smi_log><driver_version>{target['driver']}</driver_version><gpu>"
        f"<uuid>{target['uuid']}</uuid><product_name>{target['model']}</product_name>"
        f"<minor_number>0</minor_number><processes>{processes}</processes></gpu></nvidia_smi_log>"
    )
    monkeypatch.setattr(module, "query", lambda *_args: xml)
    management = configured.control.saved["device_observer"]["library"]
    engine = Engine()
    engine.path = tmp_path / "modeled-engine-identity"
    engine.path.touch()
    if occupied:
        with pytest.raises(ValueError, match="external processes"):
            module.initialize_state(root, engine, target["uuid"], management)
        assert not root.exists()
        return
    observer = module.initialize_state(root, engine, target["uuid"], management)
    mounts = json.loads((root / "mounts.json").read_text())
    assert mounts["version"] == 4 and mounts["device_observer"] == observer
    assert mounts["locks"] == [(root / "locks").stat().st_dev, (root / "locks").stat().st_ino]
    saved = json.loads((root / "journal/installation.json").read_text())
    assert saved["engine"] == engine.identifier and saved["lock_namespace"] == mounts["locks"]
    with pytest.raises(ValueError, match="new absolute"):
        module.initialize_state(root, engine, target["uuid"], management)
    assert not (root / "worker.json").exists()
