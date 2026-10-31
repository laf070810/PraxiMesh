"""Frozen single-device identity shared by task creation, OCI and old recovery."""

import json
import os
import stat
from pathlib import Path

from judge.driver_artifacts import VERSION
from judge.gpu_device_lock import GPU_UUID
from judge.sandbox import SandboxUnavailable

PROFILE = "single-cuda-device-v1"


def validate_selection(value):
    if (
        type(value) is not dict
        or set(value) != {"schema", "profile", "uuid", "driver", "minor", "uvm_major"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["profile"] != PROFILE
        or type(value["uuid"]) is not str
        or not GPU_UUID.fullmatch(value["uuid"])
        or type(value["driver"]) is not str
        or not VERSION.fullmatch(value["driver"])
        or type(value["minor"]) is not int
        or not 0 <= value["minor"] < 255
        or type(value["uvm_major"]) is not int
        or not 0 < value["uvm_major"] < 4096
        or value["uvm_major"] == 195
    ):
        raise ValueError("Invalid fixed single-GPU device selection")
    return value


def kernel_uvm_major(raw):
    if type(raw) is not str or len(raw) > 65536 or not raw.isascii():
        raise ValueError("Invalid kernel device registry")
    parts = raw.split("Block devices:")
    if len(parts) != 2 or not parts[0].startswith("Character devices:\n"):
        raise ValueError("Kernel character device registry is unavailable")
    found = [line.split() for line in parts[0].splitlines()[1:] if line.strip()]
    values = [row[0] for row in found if len(row) == 2 and row[1] == "nvidia-uvm"]
    if len(values) != 1 or not values[0].isdecimal() or not 0 < int(values[0]) < 4096:
        raise ValueError("Expected one registered NVIDIA UVM major")
    return int(values[0])


def nodes(value):
    validate_selection(value)
    return {
        f"/dev/nvidia{value['minor']}": (195, value["minor"]),
        "/dev/nvidiactl": (195, 255),
        "/dev/nvidia-uvm": (value["uvm_major"], 0),
    }


def docker_devices(value):
    return [
        {"PathOnHost": path, "PathInContainer": path, "CgroupPermissions": "rw"}
        for path in nodes(value)
    ]


def encoded_selection(value):
    return json.dumps(validate_selection(value), sort_keys=True, separators=(",", ":"))


def check_nodes(value, *, directory=Path("/dev"), exclusive=True):
    expected = nodes(value)
    if exclusive and {path.name for path in directory.glob("nvidia*")} != {
        Path(path).name for path in expected
    }:
        raise SandboxUnavailable("Task has missing or additional NVIDIA device nodes")
    for name, numbers in expected.items():
        info = (directory / Path(name).name).lstat()
        if (
            not stat.S_ISCHR(info.st_mode)
            or (os.major(info.st_rdev), os.minor(info.st_rdev)) != numbers
        ):
            raise SandboxUnavailable("Selected task device node differs from its record")


def oci_devices(value):
    devices, rules = [], [{"allow": False, "access": "rwm"}]
    for path, (major, minor) in nodes(value).items():
        devices.append(
            {
                "path": path,
                "type": "c",
                "major": major,
                "minor": minor,
                "fileMode": 0o666,
                "uid": 0,
                "gid": 0,
            }
        )
        rules.append({"allow": True, "type": "c", "major": major, "minor": minor, "access": "rw"})
    return devices, rules
