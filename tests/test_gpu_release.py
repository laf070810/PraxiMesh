"""Opaque archive fixtures only; no Docker import, code execution or GPU access."""

import copy
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge.container_controller import CONTROLLER_SOURCES, controller_digest
from judge.container_gpu import SOURCES as TASK_SOURCES
from judge.container_gpu import image_contract
from judge.container_worker_image import source_files, worker_digest
from judge.driver_artifacts import library_names
from judge.gpu_policy import EXECUTION_SOURCES, execution_digest
from judge.nvml_artifacts import manifest_digest
from judge.wasi_policy import TARGET
from scripts import deploy, release
from scripts.release_gpu import validate_metadata
from tests.test_deploy import Harness

ROOT = Path(__file__).resolve().parents[1]


def write_tar(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, payload in entries:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def write_images(package, mutate=lambda role, config: None, *, tags=None):
    contents, index = [], []
    for role in ("web", "worker", "gpu_worker", "gpu_task"):
        config = copy.deepcopy(package.configs[role])
        mutate(role, config)
        raw = json.dumps(config).encode()
        package.manifest["images"][role] = "sha256:" + hashlib.sha256(raw).hexdigest()
        contents += [(role + ".json", raw), (role + ".tar", package.layers[role])]
        index.append({"Config": role + ".json", "Layers": [role + ".tar"], "RepoTags": tags})
    contents.append(("manifest.json", json.dumps(index).encode()))
    write_tar(package.root / "images.tar", contents)


def finish(package):
    package.manifest["payloads"] = {
        name: {
            "bytes": (package.root / name).stat().st_size,
            "sha256": release.sha256(package.root / name),
        }
        for name in ("images.tar", "source.tar.gz")
    }
    (package.root / "release.json").write_text(json.dumps(package.manifest))
    return release.sha256(package.root / "release.json")


@pytest.fixture
def package(tmp_path):
    root, source = tmp_path / "package", tmp_path / "source"
    root.mkdir()
    source.mkdir()
    scopes = {
        "execution": list(EXECUTION_SOURCES),
        "controller": list(CONTROLLER_SOURCES),
        "worker": list(source_files()),
        "task": list(TASK_SOURCES),
    }
    files = {}
    for name in release.SOURCE_FILES | set().union(*map(set, scopes.values())):
        data = (ROOT / name).read_bytes()
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    driver = {
        "schema": 1,
        "profile": "cuda-ptx-driver-v1",
        "driver_version": "590.48.01",
        "libraries": {
            name: {"soname": soname, "size": 1, "sha256": "a" * 64}
            for name, soname in library_names("590.48.01").items()
        },
    }
    management = {
        "schema": 2,
        "profile": "nvidia-inventory-v2",
        "driver_version": "590.48.01",
        "library": {
            "name": "libnvidia-ml.so.590.48.01",
            "soname": "libnvidia-ml.so.1",
            "size": 1,
            "sha256": "b" * 64,
        },
        "utility": {"name": "nvidia-smi", "size": 1, "sha256": "c" * 64},
    }
    metadata = {
        "schema": 1,
        "profile": "container-single-card-v1",
        "contracts": {
            "execution": execution_digest(),
            "controller": controller_digest(),
            "worker": worker_digest(),
            "task": image_contract(driver),
        },
        "source_scopes": scopes,
        "driver": driver,
        "management": management,
    }
    manifest = {
        "format": 2,
        "platform": "linux/amd64",
        "source_id": release.identity(files),
        "source_files": files,
        "images": {},
        "gpu": metadata,
        "judge": {
            "backend": "wasi",
            "target": TARGET,
            "bundle_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
        },
    }
    result = SimpleNamespace(root=root, source=source, manifest=manifest, configs={}, layers={})
    for role in ("web", "worker", "gpu_worker", "gpu_task"):
        result.layers[role] = ("Opaque unexecuted image fixture for " + role).encode()
        config = {"User": "65532:65532", "Env": [], "Cmd": None, "Entrypoint": None}
        if role in {"web", "worker"}:
            config["Labels"] = {
                "org.praximesh.source": manifest["source_id"],
                "org.praximesh.role": role,
                "org.praximesh.wasi": "a" * 64,
                "org.praximesh.policy": "b" * 64,
            }
            config["Env"] = ["JUDGE_WASI_SHA256=" + "a" * 64]
        elif role == "gpu_worker":
            config.update(
                User="0:0",
                Entrypoint=[
                    "/usr/local/bin/python",
                    "-I",
                    "/opt/praximesh/scripts/container_worker_entry.py",
                ],
                Labels={
                    "io.praximesh.controller.contract": metadata["contracts"]["controller"],
                    "io.praximesh.worker.contract": metadata["contracts"]["worker"],
                    "io.praximesh.controller.observer": manifest_digest(management),
                },
            )
        else:
            config.update(
                Entrypoint=["/usr/local/bin/python", "-I", "/opt/praximesh/container_entry.py"],
                Labels={
                    "io.praximesh.supervision.protocol": "praximesh-container-supervision-v1",
                    "io.praximesh.supervision.runtime": metadata["contracts"]["task"],
                },
            )
        result.configs[role] = {
            "architecture": "amd64",
            "os": "linux",
            "config": config,
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + hashlib.sha256(result.layers[role]).hexdigest()],
            },
        }
    release.source_archive(source, files, root / "source.tar.gz")
    write_images(result)
    result.digest = finish(result)
    return result


def test_frozen_gpu_release_verification_never_uses_live_source_or_docker(package, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Archive verification must not execute code, query Docker or use live sources")

    monkeypatch.setattr(release, "docker", forbidden)
    monkeypatch.setattr("judge.gpu_policy.execution_digest", forbidden)
    monkeypatch.setattr("judge.container_worker_image.worker_digest", forbidden)
    monkeypatch.setattr("judge.container_controller.controller_digest", forbidden)
    assert release.verify(package.root, package.digest) == package.manifest


@pytest.mark.parametrize("value", [[], None, 1, "release"])
def test_manifest_root_must_be_an_object(package, value):
    (package.root / "release.json").write_text(json.dumps(value))
    with pytest.raises(release.ReleaseError):
        release.verify(package.root, release.sha256(package.root / "release.json"))


def test_image_inventory_must_be_a_mapping(package):
    package.manifest["images"] = list(package.manifest["images"])
    with pytest.raises(release.ReleaseError):
        release.verify(package.root, finish(package))


def test_rehashed_archived_source_must_still_match_the_frozen_gpu_contract(package):
    name = "core/gpu_worker.py"
    path = package.source / name
    path.write_bytes(path.read_bytes() + b"\n# Altered source\n")
    files = package.manifest["source_files"]
    files[name] = release.sha256(path)
    package.manifest["source_id"] = release.identity(files)
    (package.root / "source.tar.gz").unlink()
    release.source_archive(package.source, files, package.root / "source.tar.gz")
    with pytest.raises(release.ReleaseError) as error:
        release.verify(package.root, finish(package))
    assert "Archived GPU sources differ" in str(error.value.__cause__)


@pytest.mark.parametrize(
    "defect",
    [
        "schema",
        "profile",
        "digest",
        "duplicate",
        "missing-file",
        "missing-app",
        "inconsistent-scope",
        "old-management",
        "driver-mismatch",
        "private-state",
    ],
)
def test_gpu_metadata_rejects_incomplete_or_inconsistent_identity(package, defect):
    value = package.manifest["gpu"]
    if defect == "schema":
        value["schema"] = True
    elif defect == "profile":
        value["profile"] = "host-worker"
    elif defect == "digest":
        value["contracts"]["worker"] = "a" * 63
    elif defect == "duplicate":
        value["source_scopes"]["worker"].append(value["source_scopes"]["worker"][0])
    elif defect == "missing-file":
        value["source_scopes"]["task"].append("../../outside.py")
    elif defect == "missing-app":
        value["source_scopes"]["worker"].remove("community/models.py")
    elif defect == "inconsistent-scope":
        value["source_scopes"]["controller"].remove("core/gpu_worker.py")
    elif defect == "old-management":
        value["management"]["schema"] = 1
        value["management"]["profile"] = "nvml-observer-v1"
        del value["management"]["utility"]
    elif defect == "driver-mismatch":
        value["management"]["driver_version"] = "590.48.02"
        value["management"]["library"]["name"] = "libnvidia-ml.so.590.48.02"
    else:
        value["state"] = {"database_password": "synthetic-only"}
    with pytest.raises(ValueError):
        validate_metadata(value, package.manifest["source_files"])
    with pytest.raises(release.ReleaseError):
        release.verify(package.root, finish(package))


@pytest.mark.parametrize("role", ["web", "worker", "gpu_worker", "gpu_task"])
@pytest.mark.parametrize("defect", ["platform", "user", "label"])
def test_each_archived_role_is_checked_before_import(package, monkeypatch, role, defect):
    def mutate(name, config):
        if name != role:
            return
        if defect == "platform":
            config["architecture"] = "arm64"
        elif defect == "user":
            config["config"]["User"] = "1000:1000"
        else:
            config["config"]["Labels"] = {}

    write_images(package, mutate)
    monkeypatch.setattr(release, "docker", lambda *a, **k: pytest.fail("Must reject before Docker"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["release.py", "load", "--release", str(package.root), "--sha256", finish(package)],
    )
    with pytest.raises(SystemExit) as error:
        release.main()
    assert error.value.code == 1


@pytest.mark.parametrize("role", ["gpu_worker", "gpu_task"])
@pytest.mark.parametrize("field", ["Entrypoint", "Cmd"])
def test_gpu_entrypoint_and_default_arguments_are_frozen(package, role, field):
    def mutate(name, config):
        if name == role:
            config["config"][field] = ["unreviewed-command"]

    write_images(package, mutate)
    with pytest.raises(release.ReleaseError):
        release.verify(package.root, finish(package))


@pytest.mark.parametrize("defect", ["layer", "duplicate", "alias", "extra-image", "tag"])
def test_archive_cannot_rely_on_cached_layers_or_override_names(package, defect):
    if defect == "layer":
        package.layers["gpu_task"] = b"Changed layer; the old image config remains"
        write_images(package)
    elif defect == "tag":
        write_images(package, tags=["existing-unrelated-service:latest"])
    else:
        path = package.root / "images.tar"
        with tarfile.open(path, "r:") as archive:
            entries = [(m.name, archive.extractfile(m).read()) for m in archive.getmembers()]
        if defect == "duplicate":
            entries.append(entries[-1])
        elif defect == "alias":
            entries.append(("./manifest.json", entries[-1][1]))
        else:
            index = json.loads(entries[-1][1])
            index.append(index[-1])
            entries[-1] = ("manifest.json", json.dumps(index).encode())
        write_tar(path, entries)
    with pytest.raises(release.ReleaseError):
        release.verify(package.root, finish(package))


def test_import_checks_both_actual_gpu_image_roles(package, monkeypatch):
    values = {}
    for role, image in package.manifest["images"].items():
        values[image] = {
            "Id": image,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": copy.deepcopy(package.configs[role]["config"]),
        }

    monkeypatch.setattr(
        release,
        "docker",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps([values[args[-1]]]).encode()),
    )
    release.inspect_images(package.manifest)
    values[package.manifest["images"]["gpu_worker"]]["Config"]["User"] = "65532:65532"
    with pytest.raises(release.ReleaseError):
        release.inspect_images(package.manifest)


@pytest.mark.parametrize("initial", [True, False])
def test_gpu_artifact_cannot_be_silently_activated_as_cpu_only(
    package, tmp_path, monkeypatch, initial
):
    harness = Harness(tmp_path, initial=initial)
    before = copy.deepcopy(harness.state)
    monkeypatch.setattr(deploy, "verify", lambda *args: package.manifest)
    with pytest.raises(deploy.DeploymentError, match="coordinated GPU lifecycle"):
        harness.apply(package.root, package.digest)
    assert harness.state == before
    assert harness.events == []


def test_partial_gpu_build_arguments_fail_before_building(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release, "build", lambda *a, **k: pytest.fail("Incomplete GPU build must not start")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release.py",
            "build",
            "--bundle",
            str(tmp_path),
            "--bundle-sha256",
            "a" * 64,
            "--output",
            str(tmp_path / "output"),
            "--gpu-docker-socket",
            "/private/engine.sock",
        ],
    )
    with pytest.raises(SystemExit) as error:
        release.main()
    assert error.value.code == 1
