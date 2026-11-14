"""Acceptance preserves separate CPU and explicit GPU task mapping checks."""

import copy
import json
from types import SimpleNamespace

import pytest

from scripts import container_controller_acceptance as acceptance

SELECTION = {
    "schema": 1,
    "profile": "single-cuda-device-v1",
    "uuid": "GPU-fea41d22-b8d3-56e0-ff2e-40c29738bae2",
    "driver": "590.48.01",
    "minor": 2,
    "uvm_major": 507,
}
DEVICES = [
    {"PathOnHost": name, "PathInContainer": name, "CgroupPermissions": "rw"}
    for name in ("/dev/nvidia2", "/dev/nvidiactl", "/dev/nvidia-uvm")
]


def engine(monkeypatch, devices):
    observed = {
        "Mounts": [],
        "HostConfig": {"Devices": devices, "DeviceRequests": None, "NetworkMode": "none"},
        "Config": {"User": "65532:65532"},
    }
    monkeypatch.setattr(acceptance, "tree", lambda _engine, task: {task.container_id: "tree"})
    return SimpleNamespace(
        run=lambda *args: SimpleNamespace(stdout=json.dumps([observed]).encode())
    )


@pytest.mark.parametrize("gpu", [False, True])
def test_task_tree_accepts_only_its_explicit_device_mode(monkeypatch, gpu):
    actual = engine(monkeypatch, copy.deepcopy(DEVICES) if gpu else [])
    assert acceptance.task_tree(
        actual, {"container": "original"}, device_spec=SELECTION if gpu else None
    ) == {"original": "tree"}


@pytest.mark.parametrize("defect", ["missing", "extra", "wrong-card", "write-permissions"])
def test_gpu_task_tree_rejects_changed_mapping(monkeypatch, defect):
    actual = copy.deepcopy(DEVICES)
    if defect == "missing":
        actual.pop()
    elif defect == "extra":
        actual.append(
            {
                "PathOnHost": "/dev/nvidia3",
                "PathInContainer": "/dev/nvidia3",
                "CgroupPermissions": "rw",
            }
        )
    elif defect == "wrong-card":
        actual[0]["PathOnHost"] = "/dev/nvidia0"
        actual[0]["PathInContainer"] = "/dev/nvidia0"
    else:
        actual[0]["CgroupPermissions"] = "rwm"
    with pytest.raises(AssertionError):
        acceptance.task_tree(
            engine(monkeypatch, actual), {"container": "original"}, device_spec=SELECTION
        )


def test_cpu_task_tree_still_rejects_devices(monkeypatch):
    with pytest.raises(AssertionError):
        acceptance.task_tree(engine(monkeypatch, DEVICES), {"container": "original"})
