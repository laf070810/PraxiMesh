"""GPU environment preparation must reject ambiguous devices and unpinned code."""

import argparse
import os
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from judge.gpu_device_lock import DeviceLock, DeviceLockError
from scripts.gpu_environment_probe import gpu_selector, parent, runtime_from_archive, selected_cdi

UUID = "GPU-12345678-1234-1234-1234-123456789abc"
TARGET = {"index": 0, "uuid": UUID}


@pytest.mark.parametrize("value", ["all", "0,1", "GPU-short", "-1", "0\n1"])
def test_selector_rejects_ambiguous_or_invalid_devices(value):
    with pytest.raises(argparse.ArgumentTypeError):
        gpu_selector(value)


@pytest.mark.parametrize("value", ["0", UUID])
def test_selector_accepts_single_index_or_uuid(value):
    assert gpu_selector(value) == value


def fixture_cdi():
    return {
        "containerEdits": {
            "deviceNodes": [
                {"path": p} for p in ["/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia1"]
            ],
            "hooks": [
                {"path": "/usr/bin/nvidia-cdi-hook", "env": ["GOMAXPROCS=128", "OTHER=kept"]}
            ],
        },
        "devices": [
            {"name": UUID, "containerEdits": {"deviceNodes": [{"path": "/dev/nvidia0"}]}},
            {"name": "all", "containerEdits": {"deviceNodes": [{"path": "/dev/nvidia1"}]}},
        ],
    }


def test_cdi_excludes_other_devices_and_preserves_input():
    source = fixture_cdi()
    original = deepcopy(source)
    result = selected_cdi(source, TARGET)
    assert source == original
    assert [d["name"] for d in result["devices"]] == [UUID]
    edits = [result["containerEdits"], result["devices"][0]["containerEdits"]]
    assert {d["path"] for e in edits for d in e["deviceNodes"]} == {
        "/dev/nvidia0",
        "/dev/nvidiactl",
        "/dev/nvidia-uvm",
    }
    assert result["containerEdits"]["hooks"][0]["env"] == ["OTHER=kept", "GOMAXPROCS=2"]


@pytest.mark.parametrize("defect", ["missing_uuid", "duplicate_uuid", "missing_node"])
def test_incomplete_or_duplicate_cdi_is_rejected(defect):
    source = fixture_cdi()
    if defect == "missing_uuid":
        source["devices"] = []
    elif defect == "duplicate_uuid":
        source["devices"].append(deepcopy(source["devices"][0]))
    else:
        source["containerEdits"]["deviceNodes"] = []
    with pytest.raises(ValueError):
        selected_cdi(source, TARGET)


def test_unpinned_runtime_is_rejected_before_extraction(tmp_path):
    archive = tmp_path / "untrusted.tar.bz2"
    archive.write_bytes(b"This must never be extracted or executed")
    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="SHA-512"):
        runtime_from_archive(archive, destination)
    assert not destination.exists()


def test_fault_mode_requires_compiler_before_touching_device_or_starting_service(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("hardware or service must not be accessed")

    monkeypatch.setattr("scripts.gpu_environment_probe.subprocess.run", forbidden)
    with pytest.raises(ValueError, match="requires the pinned"):
        parent(SimpleNamespace(fault_probes=True, nvrtc_archive=None))


def test_duplicate_controller_is_refused_before_idle_check_or_service_start(tmp_path, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("GPU infrastructure entry requires a non-root operator")
    calls = []

    def identify(arguments, **kwargs):
        calls.append(arguments)
        assert arguments[0] == "nvidia-smi"
        assert "--query-gpu=index,uuid,name,driver_version,compute_cap" in arguments
        return subprocess.CompletedProcess(
            arguments, 0, f"0, {UUID}, fixture, 590.48.01, 8.6\n", ""
        )

    monkeypatch.setattr("scripts.gpu_environment_probe.subprocess.run", identify)
    options = SimpleNamespace(
        fault_probes=False,
        session_probes=True,
        nvrtc_archive=tmp_path / "unused",
        image_archive=None,
        gpu=UUID,
        lock_directory=tmp_path / "locks",
    )
    with DeviceLock(options.lock_directory, UUID), pytest.raises(DeviceLockError, match="Another"):
        parent(options)
    assert len(calls) == 1
