"""Snapshot installed CUDA libraries without loading them or accessing a GPU."""

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
from pathlib import Path

from judge.container_driver import ROOT, driver_digest, image_contract
from judge.driver_artifacts import (
    MAX_LIBRARY,
    PROFILE,
    file_identity,
    library_names,
    verify_bundle,
    version_from_kernel,
)
from judge.gpu_artifacts import prepare_kernel_artifacts
from scripts.container_runtime_acceptance import build_context, stage_context

KERNEL_VERSION = Path("/proc/driver/nvidia/version")


def stable_copy(source, destination, maximum, *, trusted=False):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as incoming, destination.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= maximum
            or (trusted and (before.st_uid != 0 or before.st_mode & 0o022))
        ):
            raise ValueError("Expected a bounded, trusted regular driver/artifact file")
        count = 0
        digest = hashlib.sha256()
        while chunk := incoming.read(1024 * 1024):
            count += len(chunk)
            if count > before.st_size:
                raise ValueError("Artifact changed during snapshot")
            outgoing.write(chunk)
            digest.update(chunk)
        after = os.fstat(incoming.fileno())
        if count != before.st_size or file_identity(before) != file_identity(after):
            raise ValueError("Artifact changed during snapshot")
    destination.chmod(0o644)
    return {"size": count, "sha256": digest.hexdigest()}


def elf_soname(path):
    # Static inspection only: never use ldd, ctypes or execute a driver on the host.
    with path.open("rb") as source:
        header = source.read(64)
    if (
        len(header) != 64
        or header[:7] != b"\x7fELF\x02\x01\x01"
        or struct.unpack_from("<HH", header, 16) != (3, 62)
    ):
        raise ValueError("Expected an x86_64 ELF shared library")
    result = subprocess.run(
        ["/usr/bin/readelf", "--dynamic", "--wide", str(path)],
        capture_output=True,
        check=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if len(result.stdout) > 65536 or result.stderr:
        raise ValueError("Unexpected ELF dynamic metadata")
    names = re.findall(rb"\(SONAME\)\s+Library soname: \[([^\]\n]+)\]", result.stdout)
    if len(names) != 1:
        raise ValueError("Expected exactly one driver SONAME")
    return names[0].decode("ascii")


def trusted_directory(directory):
    resolved = Path(directory).resolve(strict=True)
    if resolved not in {
        Path("/usr/lib64"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib"),
    }:
        raise ValueError("Select an installed system driver library directory")
    for path in (resolved, *resolved.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Driver directory must be owned and writable only by root")
    return resolved


def snapshot_driver(directory, destination):
    directory = trusted_directory(directory)
    kernel = KERNEL_VERSION.read_text()
    version = version_from_kernel(kernel)
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    manifest = {"schema": 1, "profile": PROFILE, "driver_version": version, "libraries": {}}
    for name, soname in library_names(version).items():
        target = destination / name
        metadata = stable_copy(directory / name, target, MAX_LIBRARY, trusted=True)
        if elf_soname(target) != soname:
            raise ValueError("Installed library SONAME does not match the CUDA/PTX profile")
        manifest["libraries"][name] = {**metadata, "soname": soname}
        if soname != name:
            (destination / soname).symlink_to(name)
    if KERNEL_VERSION.read_text() != kernel:
        raise ValueError("Kernel driver changed during snapshot")
    # Re-read installed files: reject a mixed snapshot taken during a driver upgrade.
    verify_installed(directory, manifest)
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (destination / "manifest.json").chmod(0o644)
    verify_bundle(destination, manifest)
    return manifest


def verify_installed(directory, manifest):
    from judge.driver_artifacts import validate_manifest

    validate_manifest(manifest)
    directory = trusted_directory(directory)
    if version_from_kernel(KERNEL_VERSION.read_text()) != manifest["driver_version"]:
        raise ValueError("Kernel driver no longer matches the snapshot")
    for name, identity in manifest["libraries"].items():
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != 0
                or before.st_mode & 0o022
                or before.st_size != identity["size"]
                or hashlib.file_digest(source, "sha256").hexdigest() != identity["sha256"]
                or file_identity(os.fstat(source.fileno())) != file_identity(before)
            ):
                raise ValueError("Installed driver changed since snapshot")
    if version_from_kernel(KERNEL_VERSION.read_text()) != manifest["driver_version"]:
        raise ValueError("Kernel driver changed during verification")


def build_image(
    engine, runtime_archive, nvrtc_archive, library_dir, output, tag, *, device_tasks=False
):
    if type(device_tasks) is not bool:
        raise ValueError("Explicit single-card task profile selection required")
    digest_function, image_identity = driver_digest, image_contract
    if device_tasks:
        from judge.container_gpu import gpu_task_digest
        from judge.container_gpu import image_contract as gpu_image_contract

        digest_function, image_identity = gpu_task_digest, gpu_image_contract
    expected = digest_function()
    context = stage_context(runtime_archive, output)
    manifest = snapshot_driver(library_dir, context / "driver")
    compiler = context / "compiler"
    compiler.mkdir(mode=0o700)
    archive = compiler / "nvrtc.tar.xz"
    stable_copy(nvrtc_archive, archive, 128 * 1024**2)
    compiler_identity = prepare_kernel_artifacts(compiler, archive, ROOT)
    archive.unlink()
    for name in ("driver_artifacts.py", "container_driver_guest.py"):
        shutil.copyfile(ROOT / "judge" / name, context / name)
    shutil.copyfile(
        ROOT / "scripts/container_driver_fixture.py", context / "container_driver_fixture.py"
    )
    package = json.loads((ROOT / "content/candidates/cuda.json").read_text())
    (context / "driver-cases.json").write_text(
        json.dumps(
            [{k: item[k] for k in ("id", "operation", "reference")} for item in package["problems"]]
        )
    )
    with (context / "Dockerfile").open("ab") as target:
        target.write((ROOT / "deploy/container-driver.fragment").read_bytes())
        if device_tasks:
            target.write((ROOT / "deploy/container-gpu.fragment").read_bytes())
    if device_tasks:
        # runsc binds only the selected OCI devices onto these inert mount targets.
        # Precreate them in the image so its root can remain read-only at runtime.
        mount_targets = context / "device-mounts"
        mount_targets.mkdir(mode=0o755)
        mount_targets.chmod(0o755)
        for name in [*(f"nvidia{minor}" for minor in range(255)), "nvidiactl", "nvidia-uvm"]:
            target = mount_targets / name
            target.touch(mode=0o444)
            target.chmod(0o444)
        for name in (
            "container_gpu_runtime.py",
            "container_gpu_guest.py",
            "container_kernel_entry.py",
            "container_kernel_guest.py",
        ):
            shutil.copyfile(ROOT / "judge" / name, context / name)
        shutil.copyfile(
            ROOT / "scripts/container_gpu_fixture.py", context / "container_gpu_fixture.py"
        )
    if digest_function() != expected:
        raise ValueError("Driver image source changed during staging")
    image = build_context(
        engine,
        context,
        output,
        tag,
        lambda: image_identity(manifest),
        expected_digest=image_identity(manifest),
    )
    verify_bundle(context / "driver", manifest)
    verify_installed(library_dir, manifest)
    return image, manifest, compiler_identity
