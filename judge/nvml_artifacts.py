"""Local management-library identity; independent of the CUDA task bundle."""

import hashlib
import json
import os
import stat
from pathlib import Path

from judge.driver_artifacts import SHA256, VERSION, file_identity

PROFILE = "nvml-observer-v1"
DIRECTORY = Path("/opt/praximesh/nvml")
MAX_LIBRARY = 32 * 1024**2


def library_name(version):
    if type(version) is not str or not VERSION.fullmatch(version):
        raise ValueError("Invalid management driver version")
    return f"libnvidia-ml.so.{version}"


def validate_manifest(value):
    if (
        type(value) is not dict
        or set(value) != {"schema", "profile", "driver_version", "library"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["profile"] != PROFILE
        or type(value["library"]) is not dict
    ):
        raise ValueError("Unexpected NVIDIA management manifest")
    name = library_name(value["driver_version"])
    info = value["library"]
    if (
        set(info) != {"name", "soname", "size", "sha256"}
        or info["name"] != name
        or info["soname"] != "libnvidia-ml.so.1"
        or type(info["size"]) is not int
        or not 0 < info["size"] <= MAX_LIBRARY
        or type(info["sha256"]) is not str
        or not SHA256.fullmatch(info["sha256"])
    ):
        raise ValueError("Invalid NVIDIA management library identity")
    return value


def manifest_digest(value):
    validate_manifest(value)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_manifest(directory=DIRECTORY):
    fd = os.open(Path(directory) / "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular management manifest")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Management manifest exceeded its bound")
    return validate_manifest(json.loads(raw))


def verify_bundle(expected, directory=DIRECTORY):
    directory = Path(directory)
    validate_manifest(expected)
    if read_manifest(directory) != expected:
        raise ValueError("Management library manifest changed")
    entry = expected["library"]
    if {path.name for path in directory.iterdir()} != {"manifest.json", entry["name"]}:
        raise ValueError("Unexpected management bundle content")
    fd = os.open(directory / entry["name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != entry["size"]
            or hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]
            or file_identity(os.fstat(stream.fileno())) != file_identity(before)
        ):
            raise ValueError("Management library changed")
    return manifest_digest(expected)
