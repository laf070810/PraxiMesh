"""Distinct driver-bearing task image; currently exercised without devices."""

import hashlib
from copy import deepcopy
from pathlib import Path

from judge.container_journal import PersistentRuntimeService
from judge.container_runtime_service import SOURCES as RUNTIME_SOURCES
from judge.container_service import PREFIX
from judge.driver_artifacts import manifest_digest, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = tuple(
    dict.fromkeys(
        (
            *RUNTIME_SOURCES,
            "judge/gpu_runtime.py",
            "judge/driver_artifacts.py",
            "judge/container_driver.py",
            "judge/container_driver_guest.py",
            "scripts/build_container_driver.py",
            "scripts/container_runtime_acceptance.py",
            "scripts/container_driver_fixture.py",
            "deploy/container-driver.fragment",
            "content/candidates/cuda.json",
        )
    )
)


def driver_digest():
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


def image_contract(manifest):
    return hashlib.sha256(
        b"praximesh-cuda-driver-image-v1\0"
        + driver_digest().encode()
        + b"\0"
        + manifest_digest(manifest).encode()
    ).hexdigest()


class DriverContainerService(PersistentRuntimeService):
    def __init__(self, *args, driver_manifest, **kwargs):
        self.driver_manifest = deepcopy(validate_manifest(driver_manifest))
        super().__init__(*args, **kwargs)

    def required_image_labels(self):
        return {
            **super().required_image_labels(),
            PREFIX + "runtime": image_contract(self.driver_manifest),
        }
