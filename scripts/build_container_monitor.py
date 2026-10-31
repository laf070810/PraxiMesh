"""Snapshot the local NVML management library without loading a driver."""

import hashlib
import json
import os
import stat

from judge.driver_artifacts import file_identity, version_from_kernel
from judge.nvml_artifacts import MAX_LIBRARY, PROFILE, library_name, verify_bundle
from scripts.build_container_driver import (
    KERNEL_VERSION,
    elf_soname,
    stable_copy,
    trusted_directory,
)


def snapshot_management(library_dir, destination):
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
        "schema": 1,
        "profile": PROFILE,
        "driver_version": version,
        "library": {"name": name, "soname": "libnvidia-ml.so.1", **metadata},
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (destination / "manifest.json").chmod(0o644)
    verify_bundle(manifest, destination)
    verify_installed(library_dir, manifest)
    if KERNEL_VERSION.read_text() != before:
        raise ValueError("NVIDIA kernel changed during management snapshot")
    return manifest


def verify_installed(library_dir, manifest):
    directory = trusted_directory(library_dir)
    entry = manifest["library"]
    fd = os.open(directory / entry["name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
            raise ValueError("Installed NVML changed since snapshot")
    if version_from_kernel(KERNEL_VERSION.read_text()) != manifest["driver_version"]:
        raise ValueError("NVIDIA kernel no longer matches the management snapshot")
