"""Real private metadata/locks, modeled daemon identity; no device or readiness."""

import copy
import json
import os
from types import SimpleNamespace

import pytest

from judge import container_deployment_config as metadata
from judge.container_worker_config import read_config
from judge.sandbox import SandboxUnavailable
from scripts.deploy_gpu import GPUDeploymentNode
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_container_worker_config import write_private
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import attach
from tests.test_deploy_gpu import prepared as prepared


@pytest.fixture
def bound(prepared, configuration):
    host = attach(prepared)
    binding = host.state["gpu_attachment"]["binding"]
    mounts = json.loads((prepared.root / "mounts.json").read_text())
    generation = {
        "id": "012345abcdef",
        "database": "px_example_012345abcdef",
        "gpu_worker": "px-example-012345abcdef-gpu-worker",
        "release_sha256": "d" * 64,
        "manifest": {
            "format": 2,
            "source_files": {"judge/container_deployment_config.py": "f" * 64},
            "images": {"gpu_worker": "sha256:" + "c" * 64, "gpu_task": configuration["task_image"]},
            "gpu": {
                "contracts": {"worker": "e" * 64},
                "source_scopes": {
                    scope: ["judge/container_deployment_config.py"]
                    for scope in ("execution", "controller", "worker")
                },
                "driver": copy.deepcopy(configuration["driver_manifest"]),
                "management": copy.deepcopy(mounts["device_observer"]["library"]),
            },
        },
    }
    database = {
        "url": "postgresql://px_example_worker:p%40ss@db:5432/px_example_012345abcdef",
        "sslmode": "disable",
    }

    def node():
        return GPUDeploymentNode(
            prepared.config["gpu"], prepared.directory, binding["site_id"], prepared.engine_id
        )

    with node().locked() as current:
        config, record = current.generation_configuration(binding, generation, database)
    # Rendering alone must not publish a configuration, readiness or credentials.
    assert not (prepared.root / "worker.json").exists()
    assert not (prepared.root / metadata.GENERATION).exists()
    write_private(prepared.root / "worker.json", config)
    write_private(prepared.root / metadata.GENERATION, record)
    return SimpleNamespace(
        prepared=prepared,
        host=host,
        root=prepared.root,
        binding=binding,
        generation=generation,
        database=database,
        config=config,
        record=record,
        node=node,
    )


def test_bound_snapshot_matches_original_node_generation_and_worker_contract(bound, monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("External process"))
    result = read_config(bound.root, worker_contract="e" * 64, with_generation=True)
    assert result == (bound.config, bound.record)
    assert result[0]["database"]["url"] == bound.database["url"]
    assert result[0]["enabled"] is False
    assert "p%40ss" not in json.dumps(result[1]) and "p@ss" not in json.dumps(result[1])
    assert not (bound.root / "receipts").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("worker_id", "other-worker"),
        ("task_image", "sha256:" + "f" * 64),
        (
            "database",
            {
                "url": "postgresql://px_example_worker:other@db/px_example_012345abcdef",
                "sslmode": "disable",
            },
        ),
    ],
)
def test_changed_configuration_is_not_blessed_by_old_record(bound, field, value):
    config = copy.deepcopy(bound.config)
    config[field] = value
    write_private(bound.root / "worker.json", config)
    with pytest.raises(ValueError, match="generation"):
        read_config(bound.root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("site_id", "f" * 32),
        ("binding_sha256", "f" * 64),
        ("configuration_sha256", "f" * 64),
        ("generation", "123456abcdef"),
        ("database", "px_example_123456abcdef"),
        ("release_sha256", "invalid"),
        ("worker_image", "gpu-worker:latest"),
        ("worker_contract", "invalid"),
        ("extra", True),
    ],
)
def test_record_cannot_change_generation_database_binding_or_fixed_image(bound, field, value):
    record = copy.deepcopy(bound.record)
    record[field] = value
    write_private(bound.root / metadata.GENERATION, record)
    with pytest.raises(ValueError, match="generation"):
        read_config(bound.root)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://deployment_owner:p@db/px_example_012345abcdef",
        "postgresql://px_example_web:p@db/px_example_012345abcdef",
        "postgresql://px_other_worker:p@db/px_example_012345abcdef",
        "postgresql://px_example_worker:p@db/px_example_123456abcdef",
    ],
)
def test_matching_config_hash_cannot_authorize_wrong_database_or_role(bound, url):
    config = copy.deepcopy(bound.config)
    config["database"]["url"] = url
    record = dict(bound.record, configuration_sha256=metadata.digest(config))
    write_private(bound.root / "worker.json", config)
    write_private(bound.root / metadata.GENERATION, record)
    with pytest.raises(ValueError, match="generation"):
        read_config(bound.root)


def test_verified_running_image_must_match_release_contract(bound):
    with pytest.raises(ValueError, match="image contract"):
        read_config(bound.root, worker_contract="f" * 64)


@pytest.mark.parametrize("name", [metadata.OWNER, metadata.GENERATION])
@pytest.mark.parametrize(
    "defect", ["absent", "mode", "symlink", "hardlink", "oversized", "duplicate"]
)
def test_private_generation_files_fail_closed(bound, name, defect):
    path = bound.root / name
    if defect == "absent":
        path.unlink()
    elif defect == "mode":
        path.chmod(0o644)
    elif defect == "symlink":
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    elif defect == "hardlink":
        os.link(path, path.with_suffix(".linked"))
    elif defect == "oversized":
        path.write_bytes(b" " * (metadata.MAX_METADATA + 1))
    else:
        path.write_text('{"schema":1,"schema":1}')
    with pytest.raises((ValueError, OSError, SandboxUnavailable)):
        read_config(bound.root)


@pytest.mark.parametrize(
    "defect", ["mounts", "installation", "lock", "locks-directory", "journal-directory"]
)
def test_original_mount_installation_and_lock_inodes_are_required(bound, defect):
    if defect == "mounts":
        with (bound.root / "mounts.json").open("a") as stream:
            stream.write("\n")
    elif defect == "installation":
        path = bound.root / "journal" / "installation.json"
        value = json.loads(path.read_text())
        value["installation"] = "f" * 32
        write_private(path, value)
    elif defect == "lock":
        path = bound.root / "locks" / (bound.config["gpu"] + ".lock")
        path.rename(path.with_suffix(".original"))
        path.write_text("")
        path.chmod(0o600)
    else:
        name = defect.removesuffix("-directory")
        path = bound.root / name
        path.rename(bound.root / (name + "-original"))
        path.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="Original GPU"):
        read_config(bound.root)


def test_metadata_change_during_read_is_not_accepted(bound, monkeypatch):
    original = metadata.read
    changed = False

    def read(fd, name, **kwargs):
        nonlocal changed
        value = original(fd, name, **kwargs)
        if name == metadata.GENERATION and not changed:
            changed = True
            write_private(bound.root / name, dict(bound.record, release_sha256="f" * 64))
        return value

    monkeypatch.setattr(metadata, "read", read)
    with pytest.raises(ValueError, match="changed during"):
        read_config(bound.root)


@pytest.mark.parametrize(
    "defect",
    [
        "cpu-release",
        "legacy-release",
        "missing-scope",
        "driver",
        "management",
        "database",
        "worker-id",
        "enabled",
    ],
)
def test_generation_rendering_rejects_wrong_release_node_or_worker_identity(bound, defect):
    generation, database = copy.deepcopy(bound.generation), copy.deepcopy(bound.database)
    enabled = False
    if defect == "cpu-release":
        generation["manifest"]["format"] = 1
    elif defect == "legacy-release":
        generation["manifest"]["source_files"].clear()
    elif defect == "missing-scope":
        generation["manifest"]["gpu"]["source_scopes"]["execution"].clear()
    elif defect == "driver":
        generation["manifest"]["gpu"]["driver"]["driver_version"] = "wrong-driver"
    elif defect == "management":
        generation["manifest"]["gpu"]["management"]["library"]["sha256"] = "f" * 64
    elif defect == "database":
        database["url"] = "postgresql://deployment_owner:p@db/px_example_012345abcdef"
    elif defect == "worker-id":
        generation["gpu_worker"] = "another-worker"
    else:
        enabled = "true"
    before = (bound.root / "worker.json").read_bytes()
    with bound.node().locked() as node:
        with pytest.raises(ValueError):
            node.generation_configuration(bound.binding, generation, database, enabled=enabled)
    assert (bound.root / "worker.json").read_bytes() == before


def test_rendering_copies_private_configuration_and_requires_original_lock(bound):
    node = bound.node()
    with pytest.raises(ValueError, match="held lock"):
        node.generation_configuration(bound.binding, bound.generation, bound.database)
    with node.locked():
        config, record = node.generation_configuration(
            bound.binding, bound.generation, bound.database
        )
    bound.database["url"] = "postgresql://changed:p@db/changed"
    bound.generation["manifest"]["gpu"]["driver"]["libraries"].clear()
    assert config == bound.config and record == bound.record


@pytest.mark.parametrize("raw", [b"", b"{", b"{}", b"unknown", b"{}\n"])
def test_any_unfinished_publication_marker_prevents_worker_read(bound, raw):
    path = bound.root / metadata.TRANSITION
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="publication is unfinished"):
        read_config(bound.root)


def test_transition_appearing_during_generation_read_is_refused(bound, monkeypatch):
    original = metadata.read

    def read(fd, name, **kwargs):
        value = original(fd, name, **kwargs)
        if name == metadata.GENERATION:
            path = bound.root / metadata.TRANSITION
            path.touch(mode=0o600)
        return value

    monkeypatch.setattr(metadata, "read", read)
    with pytest.raises(ValueError, match="changed during"):
        read_config(bound.root)
