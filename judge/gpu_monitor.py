"""Host-side device identity and occupancy; no CUDA context or unknown PID kill."""

import csv
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from judge.gpu_device_lock import GPU_UUID
from judge.gpu_transport import belongs_to, process_cgroup
from judge.sandbox import SandboxUnavailable


def query(*arguments):
    try:
        result = subprocess.run(
            ["nvidia-smi", *arguments],
            capture_output=True,
            text=True,
            env=dict(os.environ, LC_ALL="C"),
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SandboxUnavailable("GPU host observation failed") from None
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise SandboxUnavailable("GPU host observation is unavailable")
    return result.stdout


def identify(device_uuid):
    if type(device_uuid) is not str or not GPU_UUID.fullmatch(device_uuid):
        raise ValueError("An explicit full GPU UUID is required")
    rows = list(
        csv.reader(
            query(
                "-i",
                device_uuid,
                "--query-gpu=index,uuid,name,driver_version,compute_cap",
                "--format=csv,noheader",
            ).splitlines(),
            skipinitialspace=True,
        )
    )
    if len(rows) != 1 or len(rows[0]) != 5:
        raise SandboxUnavailable("Exactly one GPU identity is required")
    index, observed, model, driver, architecture = rows[0]
    if (
        observed != device_uuid
        or not index.isascii()
        or not index.isdecimal()
        or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", architecture)
    ):
        raise SandboxUnavailable("GPU identity does not match its configured device")
    return {
        "index": int(index),
        "uuid": observed,
        "model": model,
        "driver": driver,
        "architecture": list(map(int, architecture.split("."))),
    }


def processes(xml, target):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SandboxUnavailable("GPU process inventory is malformed") from None
    devices = root.findall("gpu")
    if (
        root.tag != "nvidia_smi_log"
        or len(devices) != 1
        or devices[0].findtext("uuid") != target["uuid"]
        or root.findtext("driver_version") != target["driver"]
        or devices[0].findtext("product_name") != target["model"]
    ):
        raise SandboxUnavailable("GPU identity changed during execution")
    inventory = devices[0].find("processes")
    if inventory is None or inventory.text and inventory.text.strip():
        raise SandboxUnavailable("GPU process inventory is unavailable")
    result = []
    for item in inventory:
        pid = item.findtext("pid", "")
        if item.tag != "process_info" or not pid.isascii() or not pid.isdecimal() or int(pid) <= 0:
            raise SandboxUnavailable("GPU process identity is unavailable")
        result.append(int(pid))
    return result


class DeviceMonitor:
    def __init__(self, target):
        self.target = dict(target)

    def observe(self, service=None):
        pids = processes(query("-i", self.target["uuid"], "--query", "--xml-format"), self.target)
        if pids and service is None:
            raise SandboxUnavailable("Selected GPU is occupied; nothing was stopped")
        for pid in pids:
            if not belongs_to(process_cgroup(pid), service.cgroup_name):
                raise SandboxUnavailable("GPU has an external process; discard this evaluation")
        return pids
