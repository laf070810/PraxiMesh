"""Validate a pinned NVIDIA full query and retain only device identity and PIDs."""

import re
import xml.etree.ElementTree as ET

from judge.container_processes import MAX_PIDS
from judge.sandbox import SandboxUnavailable

MAX_XML = 1024 * 1024


def utility_version(raw, expected):
    try:
        if type(raw) is not bytes or len(raw) > 4096:
            raise ValueError
        fields = {}
        for line in raw.decode("ascii").strip().splitlines():
            key, value = (part.strip() for part in line.split(":", 1))
            if (
                key in fields
                or not key
                or not (
                    re.fullmatch(r"[0-9.]{1,64}", value)
                    or (key == "CUDA Version" and value == "N/A")
                )
            ):
                raise ValueError
            fields[key] = value
        if (
            set(fields) != {"NVIDIA-SMI version", "NVML version", "DRIVER version", "CUDA Version"}
            or fields["NVIDIA-SMI version"] != expected
            or fields["DRIVER version"] != expected
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise SandboxUnavailable(
            "NVIDIA inventory utility version does not match its driver"
        ) from None
    return expected


def field(parent, name):
    values = parent.findall(name)
    if len(values) != 1 or len(values[0]) or not values[0].text:
        raise ValueError
    return values[0].text.strip()


def inventory(raw, saved):
    """Every process counts, including Other, combined and unknown future types."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_XML:
            raise ValueError
        # ElementTree does not load an external DTD. Permit only NVIDIA's fixed
        # external declaration, never an internal subset or entity expansion.
        cleaned = re.sub(
            rb'<!DOCTYPE nvidia_smi_log SYSTEM "nvsmi_device_v[0-9]+\.dtd">', b"", raw, count=1
        )
        if b"<!DOCTYPE" in cleaned or b"<!ENTITY" in cleaned:
            raise ValueError
        root = ET.fromstring(cleaned)
        devices = root.findall("gpu")
        if root.tag != "nvidia_smi_log" or len(devices) != 1:
            raise ValueError
        target, gpu = saved["target"], devices[0]
        minor = field(gpu, "minor_number")
        if (
            field(root, "driver_version") != target["driver"]
            or field(gpu, "uuid") != target["uuid"]
            or field(gpu, "product_name") != target["model"]
            or not minor.isascii()
            or not minor.isdecimal()
            or int(minor) != saved["minor"]
        ):
            raise ValueError
        found = gpu.findall("processes")
        if len(found) != 1 or (found[0].text or "").strip() or len(found[0]) > MAX_PIDS:
            raise ValueError
        pids = set()
        for item in found[0]:
            pid = field(item, "pid")
            if (
                item.tag != "process_info"
                or (item.text or "").strip()
                or (item.tail or "").strip()
                or not pid.isascii()
                or not pid.isdecimal()
                or not 0 < int(pid) < 2**31
                or int(pid) in pids
            ):
                raise ValueError
            pids.add(int(pid))
        return sorted(pids)
    except (ValueError, ET.ParseError):
        raise SandboxUnavailable(
            "NVIDIA full process inventory is unavailable or changed"
        ) from None
