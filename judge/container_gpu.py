"""Single-card task service; distinct from device-free driver/compile profiles."""

import hashlib
from contextlib import contextmanager
from copy import deepcopy

from judge.container_contract import WORKLOAD_PROFILE
from judge.container_devices import docker_devices, validate_selection
from judge.container_driver import ROOT, DriverContainerService
from judge.container_driver import SOURCES as DRIVER_SOURCES
from judge.driver_artifacts import manifest_digest
from judge.sandbox import SandboxUnavailable

SOURCES = tuple(
    dict.fromkeys(
        (
            *DRIVER_SOURCES,
            "judge/container_gpu.py",
            "judge/container_gpu_runtime.py",
            "judge/container_gpu_guest.py",
            "judge/container_kernel_entry.py",
            "judge/container_kernel_guest.py",
            "judge/container_execution.py",
            "scripts/container_gpu_fixture.py",
            "deploy/container-gpu.fragment",
        )
    )
)


def gpu_task_digest():
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


def image_contract(manifest):
    return hashlib.sha256(
        b"praximesh-single-gpu-task-v1\0"
        + gpu_task_digest().encode()
        + b"\0"
        + manifest_digest(manifest).encode()
    ).hexdigest()


class GPUContainerService(DriverContainerService):
    def __init__(self, *args, device_spec, **kwargs):
        self.device_spec = deepcopy(validate_selection(device_spec))
        kwargs.setdefault("resource_profile", WORKLOAD_PROFILE)
        super().__init__(*args, **kwargs)
        if (
            self.resource_profile != WORKLOAD_PROFILE
            or self.device_spec["uuid"] != self.guard.device_uuid
            or self.device_spec["driver"] != self.driver_manifest["driver_version"]
        ):
            raise SandboxUnavailable("GPU task image, lock or total budget mismatch")

    def required_image_labels(self):
        from judge.container_service import PREFIX

        return {
            **super().required_image_labels(),
            PREFIX + "runtime": image_contract(self.driver_manifest),
        }

    def journal_device_spec(self):
        return deepcopy(self.device_spec)

    def valid_devices(self, value):
        return value == docker_devices(self.device_spec)

    @contextmanager
    def creation_options(self):
        with super().creation_options() as options:
            yield (
                *options,
                *(
                    "--device=" + item["PathOnHost"] + ":" + item["PathInContainer"] + ":rw"
                    for item in docker_devices(self.device_spec)
                ),
            )
