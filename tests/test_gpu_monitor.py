"""Synthetic NVML observations; no device access or GPU recovery claims."""

import os
from types import SimpleNamespace

import pytest

from judge import gpu_monitor
from judge.gpu_monitor import DeviceMonitor, processes
from judge.gpu_transport import process_cgroup
from judge.sandbox import SandboxUnavailable

TARGET = {
    "uuid": "GPU-11111111-2222-3333-4444-555555555555",
    "driver": "590.48.01",
    "model": "fixture",
}


def inventory(rows=""):
    return f"<nvidia_smi_log><driver_version>{TARGET['driver']}</driver_version><gpu><uuid>{TARGET['uuid']}</uuid><product_name>fixture</product_name><processes>{rows}</processes></gpu></nvidia_smi_log>"


def test_compute_graphics_and_other_processes_all_count_as_occupation():
    xml = inventory(
        "".join(
            f"<process_info><pid>{n}</pid><type>{kind}</type></process_info>"
            for n, kind in enumerate(["C", "G", "O"], 1)
        )
    )
    assert processes(xml, TARGET) == [1, 2, 3]
    assert processes(inventory(), TARGET) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.replace("590.48.01", "999"),
        lambda v: v.replace(TARGET["uuid"], "wrong-device"),
        lambda v: v.replace("fixture", "different-model"),
        lambda v: v.replace("<processes></processes>", ""),
        lambda v: v.replace("<processes></processes>", "<processes>N/A</processes>"),
        lambda v: v.replace(
            "<processes></processes>",
            "<processes><process_info><pid>N/A</pid></process_info></processes>",
        ),
    ],
)
def test_changed_or_unknown_inventory_fails_closed(change):
    with pytest.raises(SandboxUnavailable):
        processes(change(inventory()), TARGET)


def test_only_processes_in_the_owned_cgroup_are_allowed(monkeypatch):
    monkeypatch.setattr(
        gpu_monitor,
        "query",
        lambda *args: inventory(
            f"<process_info><pid>{os.getpid()}</pid><type>G</type></process_info>"
        ),
    )
    monitor = DeviceMonitor(TARGET)
    with pytest.raises(SandboxUnavailable, match="occupied"):
        monitor.observe()
    service = SimpleNamespace(cgroup_name=process_cgroup(os.getpid()))
    assert monitor.observe(service) == [os.getpid()]
    service.cgroup_name += "/another-service"
    with pytest.raises(SandboxUnavailable, match="external process"):
        monitor.observe(service)
