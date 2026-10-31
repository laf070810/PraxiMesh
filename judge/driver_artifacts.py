"""Versioned local CUDA/PTX library snapshot; never a GPU readiness receipt."""

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from pathlib import Path

PROFILE = "cuda-ptx-driver-v1"
MAX_LIBRARY = 256 * 1024**2
VERSION = re.compile(r"[0-9]{3}\.[0-9]{2}\.[0-9]{2}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LIBRARIES = {
    "libcuda.so": "libcuda.so.1",
    "libnvidia-ptxjitcompiler.so": "libnvidia-ptxjitcompiler.so.1",
    "libnvidia-nvvm.so": "libnvidia-nvvm.so.4",
    "libnvidia-gpucomp.so": None,
}


def file_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def version_from_kernel(text):
    match = re.fullmatch(
        r"NVRM version: NVIDIA UNIX Open Kernel Module for x86_64\s+"
        r"([0-9]{3}\.[0-9]{2}\.[0-9]{2})\s+[^\n]+\nGCC version: [^\n]+\n?",
        text,
    )
    if not match:
        raise ValueError("Expected an identified NVIDIA open x86_64 kernel driver")
    return match[1]


def library_names(version):
    if type(version) is not str or not VERSION.fullmatch(version):
        raise ValueError("Invalid NVIDIA driver version")
    return {
        f"{stem}.{version}": soname or f"{stem}.{version}" for stem, soname in LIBRARIES.items()
    }


def validate_manifest(value):
    if (
        type(value) is not dict
        or set(value) != {"schema", "profile", "driver_version", "libraries"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["profile"] != PROFILE
        or type(value["libraries"]) is not dict
    ):
        raise ValueError("Unexpected CUDA driver manifest")
    expected = library_names(value["driver_version"])
    if set(value["libraries"]) != set(expected):
        raise ValueError("Incomplete CUDA/PTX library manifest")
    for name, soname in expected.items():
        info = value["libraries"][name]
        if (
            type(info) is not dict
            or set(info) != {"soname", "size", "sha256"}
            or info["soname"] != soname
            or type(info["size"]) is not int
            or not 0 < info["size"] <= MAX_LIBRARY
            or type(info["sha256"]) is not str
            or not SHA256.fullmatch(info["sha256"])
        ):
            raise ValueError("Invalid CUDA/PTX library identity")
    return value


def manifest_digest(value):
    validate_manifest(value)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prepare_request(manifest):
    return {
        "protocol": 1,
        "operation": "prepare-driver",
        "manifest": deepcopy(validate_manifest(manifest)),
    }


def verify_bundle(directory, expected):
    """Verify all regular files and exact aliases before any library is loaded."""
    validate_manifest(expected)
    directory = Path(directory)
    path = directory / "manifest.json"
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 8192:
        raise ValueError("Invalid driver manifest file")
    actual = json.loads(path.read_text())
    if manifest_digest(actual) != manifest_digest(expected):
        raise ValueError("Driver manifest changed")
    names = {"manifest.json"}
    for name, info in expected["libraries"].items():
        names.add(name)
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != info["size"]:
                raise ValueError("Driver library type or size changed")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
            if file_identity(before) != file_identity(after) or digest != info["sha256"]:
                raise ValueError("Driver library changed")
        if info["soname"] != name:
            alias = directory / info["soname"]
            names.add(alias.name)
            if not alias.is_symlink() or os.readlink(alias) != name:
                raise ValueError("Driver SONAME alias changed")
    if {item.name for item in directory.iterdir()} != names:
        raise ValueError("Unexpected file in driver bundle")
    return manifest_digest(expected)
