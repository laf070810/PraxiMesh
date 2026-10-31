"""Snapshot the local NVML management library without loading a driver."""

import hashlib
import json
import os
import stat
import struct
from pathlib import Path

from judge.driver_artifacts import file_identity, version_from_kernel
from judge.nvml_artifacts import (
    INVENTORY_PROFILE,
    MAX_LIBRARY,
    MAX_UTILITY,
    PROFILE,
    library_name,
    validate_manifest,
    verify_bundle,
)
from scripts.build_container_driver import (
    KERNEL_VERSION,
    elf_soname,
    stable_copy,
    trusted_directory,
)

UTILITY = Path("/usr/bin/nvidia-smi")


def trusted_utility():
    for path in UTILITY.parents:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("NVIDIA utility directory must be writable only by root")
    return UTILITY


def snapshot_management(library_dir, destination, *, full_inventory=False):
    if type(full_inventory) is not bool:
        raise ValueError("Explicit inventory profile selection required")
    directory = trusted_directory(library_dir)
    before = KERNEL_VERSION.read_text()
    version = version_from_kernel(before)
    name = library_name(version)
    destination.mkdir(mode=0o755)
    target = destination / name
    metadata = stable_copy(directory / name, target, MAX_LIBRARY, trusted=True)
    if elf_soname(target) != "libnvidia-ml.so.1":
        raise ValueError("Installed NVML SONAME does not match the management profile")
    manifest = {
        "schema": 2 if full_inventory else 1,
        "profile": INVENTORY_PROFILE if full_inventory else PROFILE,
        "driver_version": version,
        "library": {"name": name, "soname": "libnvidia-ml.so.1", **metadata},
    }
    if full_inventory:
        utility = destination / "nvidia-smi"
        info = stable_copy(trusted_utility(), utility, MAX_UTILITY, trusted=True)
        with utility.open("rb") as source:
            header = source.read(64)
        if (
            len(header) != 64
            or header[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", header, 16)[0] not in {2, 3}
            or struct.unpack_from("<H", header, 18)[0] != 62
        ):
            raise ValueError("Expected a native x86_64 NVIDIA query executable")
        utility.chmod(0o755)
        manifest["utility"] = {"name": "nvidia-smi", **info}
        (destination / "libnvidia-ml.so.1").symlink_to(name)
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (destination / "manifest.json").chmod(0o644)
    verify_bundle(manifest, destination)
    verify_installed(library_dir, manifest)
    if KERNEL_VERSION.read_text() != before:
        raise ValueError("NVIDIA kernel changed during management snapshot")
    return manifest


def verify_installed(library_dir, manifest):
    validate_manifest(manifest)
    directory = trusted_directory(library_dir)
    entries = [(directory / manifest["library"]["name"], manifest["library"])]
    if manifest["schema"] == 2:
        entries.append((trusted_utility(), manifest["utility"]))
    for path, entry in entries:
        verify_installed_file(path, entry)
    if version_from_kernel(KERNEL_VERSION.read_text()) != manifest["driver_version"]:
        raise ValueError("NVIDIA kernel no longer matches the management snapshot")


def verify_installed_file(path, entry):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_size != entry["size"]
            or hashlib.file_digest(source, "sha256").hexdigest() != entry["sha256"]
            or file_identity(os.fstat(source.fileno())) != file_identity(info)
        ):
            raise ValueError("Installed NVIDIA management artifact changed since snapshot")
