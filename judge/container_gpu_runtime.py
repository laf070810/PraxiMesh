"""Explicit nested nvproxy runtime with one selected card and no automatic injection."""

import re
from copy import deepcopy
from pathlib import Path

from judge.container_contract import PYTHON, WORKLOAD_PROFILE
from judge.container_devices import check_nodes, kernel_uvm_major, oci_devices, validate_selection
from judge.container_oci import ContainerRuntime
from judge.driver_artifacts import verify_bundle, version_from_kernel
from judge.gpu_task_protocol import validate_target
from judge.sandbox import SandboxUnavailable


class GPUContainerRuntime(ContainerRuntime):
    def __init__(self, *, device_spec, driver_manifest, target=None, **kwargs):
        self.device_spec = deepcopy(validate_selection(device_spec))
        self.driver_manifest = deepcopy(driver_manifest)
        self.target = deepcopy(target)
        self.kernel_cleanup = []
        if target is not None:
            validate_target(target)
            if target["uuid"] != device_spec["uuid"] or target["driver"] != device_spec["driver"]:
                raise SandboxUnavailable("Kernel target does not match its selected device")
        super().__init__(**kwargs)
        if self.resource_profile != WORKLOAD_PROFILE:
            raise SandboxUnavailable("GPU task requires the fixed whole-task resource budget")
        if (
            version_from_kernel(Path("/proc/driver/nvidia/version").read_text())
            != self.device_spec["driver"]
        ):
            raise SandboxUnavailable("Task kernel driver does not match its selected device")
        if kernel_uvm_major(Path("/proc/devices").read_text()) != self.device_spec["uvm_major"]:
            raise SandboxUnavailable("NVIDIA UVM registration changed")
        if self.driver_manifest["driver_version"] != self.device_spec["driver"]:
            raise SandboxUnavailable("Task driver snapshot does not match its selected device")
        verify_bundle(Path("/opt/guest-root/opt/driver"), self.driver_manifest)
        check_nodes(self.device_spec)
        versions = re.findall(
            r"\b[0-9]{3}\.[0-9]{2}\.[0-9]{2}\b",
            self.command("nvproxy", "list-supported-drivers").stdout.decode(),
        )
        if self.device_spec["driver"] not in versions:
            raise SandboxUnavailable("Pinned runsc does not support the exact task driver")

    def tick(self):
        super().tick()
        check_nodes(self.device_spec)

    def kernel_session(self, label, *, mode, on_tick=None):
        if self.target is None:
            raise SandboxUnavailable("An explicit kernel target is required")
        self.create(label, PYTHON, ["-I", "/opt/praximesh/judge/container_kernel_guest.py"])
        return self.session(
            label,
            mode=mode,
            on_tick=on_tick or self.tick,
            on_cleanup=self.kernel_cleanup.append,
            architecture=self.target["architecture"],
        )

    def arguments(self, *args):
        return super().arguments(
            "--nvproxy=true", "--nvproxy-allowed-driver-capabilities=compute,utility", *args
        )

    def specification(self, command):
        value = super().specification(command)
        devices, rules = oci_devices(self.device_spec)
        value["linux"]["devices"] = devices
        value["linux"]["resources"]["devices"] = rules
        value["process"]["env"] += [
            "NVIDIA_VISIBLE_DEVICES=void",
            "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
        ]
        return value
