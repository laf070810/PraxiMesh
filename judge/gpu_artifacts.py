"""Pinned offline artifacts shared by CUDA controllers and infrastructure probes."""

import hashlib
import json
import re
import shutil
import tarfile
from copy import deepcopy

RELEASE = "release-20261026.0"
ARCHIVE_SHA512 = "26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a"
RUNSC_SHA256 = "7d658f2c3a5602d28c391285eb4ee0740e7dd21d40731324049a29620e51cf5e"


IMAGE = "docker.io/library/python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15"
NVRTC_ARCHIVE_SHA256 = "86077f16674ccea3e621ade9678887a37aca8f7500091aee4cabb1915e0c527b"
NVRTC_DIRECTORY = "cuda_nvrtc-linux-x86_64-13.1.115-archive"
LIBRARIES = {
    "libnvrtc.so.13.1.115": "f0ca45d23836e8028d51a60db9d825540e9e6cfe7491507c804ad8b9beb368a7",
    "libnvrtc-builtins.so.13.1.115": "b38e97128bba09c2bfd7641179f5f5211ab6a8fc18bb497d89ea02cb6806a5b0",
}
ALIASES = {
    "libnvrtc.so": "libnvrtc.so.13",
    "libnvrtc.so.13": "libnvrtc.so.13.1.115",
    "libnvrtc-builtins.so": "libnvrtc-builtins.so.13.1",
    "libnvrtc-builtins.so.13.1": "libnvrtc-builtins.so.13.1.115",
}
HARNESS = ("__init__.py", "gpu_contract.py", "gpu_runtime.py", "results.py")


def verify_image_archive(archive):
    """Validate the complete pinned OCI graph without extracting image layers."""
    if archive.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("GPU image archive exceeds its size limit")
    with tarfile.open(archive, "r:") as bundle:
        files, names = {}, set()
        for member in bundle:
            name = member.name.rstrip("/")
            if len(names) >= 12:
                raise ValueError("Too many OCI archive members")
            if name in names:
                raise ValueError("Duplicate OCI archive member")
            names.add(name)
            if member.isdir() and name in {"blobs", "blobs/sha256"}:
                continue
            if not member.isfile() or not (
                name in {"index.json", "oci-layout"}
                or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name)
            ):
                raise ValueError("Unexpected OCI archive member")
            files[name] = member
        if sum(member.size for member in files.values()) > 128 * 1024 * 1024:
            raise ValueError("GPU image archive contents exceed their size limit")

        def read_json(name):
            if name not in files or files[name].size > 64 * 1024:
                raise ValueError("Missing or oversized OCI metadata")
            return json.load(bundle.extractfile(files[name]))

        if read_json("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Unexpected OCI layout version")
        index = read_json("index.json")
        pinned = IMAGE.split("@", 1)[1]
        if (
            index.get("schemaVersion") != 2
            or type(index.get("manifests")) is not list
            or len(index["manifests"]) != 1
            or index["manifests"][0].get("digest") != pinned
        ):
            raise ValueError("OCI archive must contain the pinned GPU image manifest")
        manifest_name = "blobs/" + pinned.replace(":", "/")
        manifest = read_json(manifest_name)
        descriptors = [index["manifests"][0], manifest["config"], *manifest["layers"]]
        referenced = {"index.json", "oci-layout"}
        for descriptor in descriptors:
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Unsupported OCI blob digest")
            name = "blobs/" + digest.replace(":", "/")
            referenced.add(name)
            if name not in files or files[name].size != descriptor["size"]:
                raise ValueError("Missing or resized OCI blob")
            with bundle.extractfile(files[name]) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest.split(":", 1)[1]:
                raise ValueError("OCI blob digest mismatch")
        if set(files) != referenced:
            raise ValueError("Unreferenced OCI archive content")
        image_id = manifest["config"]["digest"]
        config = read_json("blobs/" + image_id.replace(":", "/"))
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("GPU probe requires the pinned Linux amd64 image")
    return {
        "image_archive_sha256": sha256(archive),
        "image_manifest": pinned,
        "image_id": image_id,
    }


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_kernel_artifacts(base, archive, root):
    """Verify before extraction; mount neither fixtures nor expected values."""
    if sha256(archive) != NVRTC_ARCHIVE_SHA256:
        raise ValueError("NVRTC archive must match the pinned 13.1.115 SHA-256")
    extracted = base / "nvrtc-extracted"
    extracted.mkdir(mode=0o700)
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted, filter="data")
        source = extracted / NVRTC_DIRECTORY
        destination = base / "nvrtc"
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        for filename, expected in LIBRARIES.items():
            path = source / "lib" / filename
            if sha256(path) != expected:
                raise ValueError("NVRTC library digest mismatch")
            shutil.copyfile(path, destination / filename)
            (destination / filename).chmod(0o644)
        for filename, target in ALIASES.items():
            (destination / filename).symlink_to(target)
        shutil.copyfile(source / "LICENSE", destination / "LICENSE")
        (destination / "LICENSE").chmod(0o644)
    finally:
        shutil.rmtree(extracted)
    harness = base / "harness/judge"
    harness.mkdir(mode=0o755, parents=True)
    harness.chmod(0o755)
    harness.parent.chmod(0o755)
    for filename in HARNESS:
        shutil.copyfile(root / "judge" / filename, harness / filename)
        (harness / filename).chmod(0o644)
    return {
        "nvrtc_archive_sha256": NVRTC_ARCHIVE_SHA256,
        "nvrtc_libraries": dict(LIBRARIES),
        "harness_sha256": {filename: sha256(harness / filename) for filename in HARNESS},
    }


def container_options(base, *, diagnostics=False):
    options = [
        "--volume=" + str(base / "harness") + ":/opt/praximesh:ro",
        "--volume=" + str(base / "nvrtc") + ":/opt/nvrtc:ro",
        "--workdir=/opt/praximesh",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=LD_LIBRARY_PATH=/opt/nvrtc:/usr/lib64",
    ]
    if diagnostics:
        # Only reviewed synthetic fixtures run here. Diagnostics remain in the
        # private probe directory, never a learner response or formal evidence.
        options.append("--env=PRAXIMESH_GPU_DIAGNOSTICS=1")
    return options


def runtime_from_archive(archive, destination):
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha512").hexdigest()
    if digest != ARCHIVE_SHA512:
        raise ValueError(f"Archive must match the pinned {RELEASE} SHA-512")
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    with (destination / "runsc").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != RUNSC_SHA256 or not (destination / "gvisor-bin").is_dir():
        raise ValueError("Pinned runsc and its sidecar directory are required")
    return digest


def selected_cdi(spec, target):
    spec = deepcopy(spec)
    spec["devices"] = [d for d in spec["devices"] if d["name"] == target["uuid"]]
    if len(spec["devices"]) != 1:
        raise ValueError("CDI must contain exactly one matching GPU UUID")
    gpu_node = f"/dev/nvidia{target['index']}"
    allowed = {gpu_node, "/dev/nvidiactl", "/dev/nvidia-uvm"}
    selected = set()
    for edits in [spec["containerEdits"], spec["devices"][0]["containerEdits"]]:
        edits["deviceNodes"] = [d for d in edits.get("deviceNodes", []) if d["path"] in allowed]
        selected.update(d["path"] for d in edits["deviceNodes"])
        for hook in edits.get("hooks", []):
            hook["env"] = [
                value for value in hook.get("env", []) if not value.startswith("GOMAXPROCS=")
            ] + ["GOMAXPROCS=2"]
    if selected != allowed:
        raise ValueError("CDI is missing a required GPU device node")
    return spec
