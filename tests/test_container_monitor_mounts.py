"""Explicit v3 selection must be verified before acquiring task state."""

import copy
import json

import pytest

from judge import container_controller as module
from judge.container_controller import ContainerController
from judge.sandbox import SandboxUnavailable
from tests.test_container_controller import mounts  # noqa: F401
from tests.test_container_monitor import bundle, saved  # noqa: F401
from tests.test_smi_inventory import upgrade


def configure(mounts, saved, monkeypatch):  # noqa: F811
    calls = []
    saved["target"]["uuid"] = mounts.device
    mounts.value.update(
        version=saved["schema"] + 2,
        host_processes={
            "version": 1,
            "root": [22, 1],
            "boot": "11111111-2222-3333-4444-555555555555",
        },
        device_observer=copy.deepcopy(saved),
    )

    class View:
        def __init__(self, _saved):
            calls.append("open")

        def check(self):
            calls.append("proc")

        def close(self):
            calls.append("close")

    def check(value, view):
        assert value == saved and isinstance(view, View)
        calls.append("nvml")

    monkeypatch.setattr(module, "HostProcessView", View)
    monkeypatch.setattr(module, "check_prerequisites", check)
    return calls


def test_v3_rechecks_both_identities(mounts, saved, monkeypatch):  # noqa: F811
    calls = configure(mounts, saved, monkeypatch)
    mounts.path.write_text(json.dumps(mounts.value))
    with ContainerController(mounts.device) as control:
        control.check()
    assert calls[0] == "open" and calls[-1] == "close"
    assert calls.count("nvml") >= 3 and calls.count("proc") >= calls.count("nvml")


@pytest.mark.parametrize(
    "defect", ["missing-proc", "missing-observer", "driver", "device", "extra"]
)
def test_incomplete_v3_fails_before_opening_procfs_or_engine(mounts, saved, monkeypatch, defect):  # noqa: F811
    calls = configure(mounts, saved, monkeypatch)
    if defect == "missing-proc":
        mounts.value.pop("host_processes")
    elif defect == "missing-observer":
        mounts.value.pop("device_observer")
    elif defect == "driver":
        mounts.value["device_observer"]["target"]["driver"] = "999.99.99"
    elif defect == "device":
        mounts.value["device_observer"]["target"]["uuid"] = (
            "GPU-11111111-2222-3333-4444-555555555555"
        )
    else:
        mounts.value["capabilities"] = "all"
    mounts.path.write_text(json.dumps(mounts.value))
    with pytest.raises(SandboxUnavailable):
        ContainerController(mounts.device)
    assert calls == [] and mounts.fake.calls == []


def test_bad_device_files_close_procfs_without_opening_task_state(mounts, saved, monkeypatch):  # noqa: F811
    calls = configure(mounts, saved, monkeypatch)
    mounts.path.write_text(json.dumps(mounts.value))

    def fail(*_args):
        raise SandboxUnavailable("Wrong device files")

    monkeypatch.setattr(module, "check_prerequisites", fail)
    with pytest.raises(SandboxUnavailable, match="device files"):
        ContainerController(mounts.device)
    assert calls[-1] == "close" and mounts.fake.calls == []


def test_v4_rechecks_full_identity(mounts, saved, monkeypatch):  # noqa: F811
    upgrade(saved)
    calls = configure(mounts, saved, monkeypatch)
    mounts.path.write_text(json.dumps(mounts.value))
    with ContainerController(mounts.device) as control:
        assert control.saved["version"] == 4
        control.check()
    assert calls.count("nvml") >= 3 and calls[-1] == "close"


@pytest.mark.parametrize("full", [True, False])
def test_mount_profile_cannot_downgrade(mounts, saved, monkeypatch, full):  # noqa: F811
    if full:
        upgrade(saved)
    calls = configure(mounts, saved, monkeypatch)
    mounts.value["version"] = 3 if full else 4
    mounts.path.write_text(json.dumps(mounts.value))
    with pytest.raises(SandboxUnavailable, match="profiles differ"):
        ContainerController(mounts.device)
    assert calls == [] and mounts.fake.calls == []
