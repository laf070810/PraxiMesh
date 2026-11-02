"""Local image selection cannot be a receipt, mutable tag or alternate runtime."""

import copy
import json
from types import SimpleNamespace

import pytest

from judge import container_practice_bundle as module
from judge.container_contract import ENTRY, PROTOCOL, PYTHON
from judge.container_controller import controller_digest
from judge.container_gpu import gpu_task_digest, image_contract
from judge.container_preparation import create_json
from judge.container_service import PREFIX
from judge.gpu_policy import execution_digest
from judge.nvml_artifacts import manifest_digest
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle


@pytest.fixture
def images(configured):
    adapter = configured.adapter
    value = {
        "format": 1,
        "kind": module.KIND,
        "controller_image": "sha256:" + "d" * 64,
        "task_image": adapter.image,
        "controller_contract": controller_digest(),
        "task_contract": gpu_task_digest(),
        "execution_digest": execution_digest(),
        "driver_manifest": adapter.driver_manifest,
        "management_manifest": adapter.environment["artifacts"]["management_manifest"],
    }
    records = {
        value["controller_image"]: {
            "Entrypoint": [PYTHON, "-I", "/opt/praximesh/scripts/container_controller_fixture.py"],
            "User": "0:0",
            "WorkingDir": "/opt/praximesh",
            "Env": [],
            "Labels": {
                "io.praximesh.controller.contract": controller_digest(),
                "io.praximesh.controller.observer": manifest_digest(value["management_manifest"]),
            },
        },
        value["task_image"]: {
            "Entrypoint": [PYTHON, "-I", ENTRY],
            "Env": [],
            "Labels": {
                PREFIX + "protocol": PROTOCOL,
                PREFIX + "runtime": image_contract(value["driver_manifest"]),
            },
        },
    }
    calls = []

    def run(*args):
        calls.append(args)
        assert args[:2] == ("image", "inspect")
        return SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "Id": args[2],
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": records[args[2]],
                    }
                ]
            ).encode()
        )

    return SimpleNamespace(
        value=value, records=records, calls=calls, engine=SimpleNamespace(run=run)
    )


def test_private_manifest_only_inspects_images_and_recovery_does_not_need_task(images, tmp_path):
    path = tmp_path / "local-gpu.json"
    create_json(path, images.value)
    original = copy.deepcopy(images.value)
    assert module.verify_images(images.engine, module.read(path)) == original
    assert len(images.calls) == 2 and images.value == original
    del images.records[images.value["task_image"]]
    assert module.verify_images(images.engine, original, task=False) == original
    assert images.calls[-1][-1] == original["controller_image"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", True),
        ("ready", True),
        ("kind", "receipt"),
        ("controller_image", "latest"),
        ("task_image", "a:tag"),
        ("controller_contract", "a" * 64),
        ("task_contract", "a" * 64),
        ("execution_digest", "a" * 64),
    ],
)
def test_bundle_rejects_stale_or_retyped_identity_before_engine(images, field, value):
    images.value[field] = value
    with pytest.raises((SandboxUnavailable, ValueError)):
        module.verify_images(images.engine, images.value)
    assert not images.calls


@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink", "oversize"])
def test_private_manifest_file_identity(images, tmp_path, kind):
    path = tmp_path / "images.json"
    create_json(path, images.value)
    if kind == "symlink":
        target = tmp_path / "alias"
        target.symlink_to(path)
        path = target
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "hardlink":
        (tmp_path / "alias").hardlink_to(path)
    else:
        path.write_bytes(b"x" * (module.MAX_RECORD + 1))
    with pytest.raises((OSError, ValueError, SandboxUnavailable)):
        module.read(path)


@pytest.mark.parametrize("role", ["controller", "task"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("Entrypoint", ["sh"]),
        ("Env", "PATH=elsewhere"),
        ("Labels", {}),
        ("Healthcheck", {"Test": ["CMD", "true"]}),
        ("Volumes", {"/data": {}}),
    ],
)
def test_image_contract_rejects_alternate_execution(images, role, field, value):
    images.records[images.value[role + "_image"]][field] = value
    with pytest.raises(SandboxUnavailable):
        module.verify_images(images.engine, images.value)
