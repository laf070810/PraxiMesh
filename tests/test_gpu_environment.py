"""Private runtime boundary checks; actual runsc proof uses its separate script."""

import os
from copy import deepcopy

import pytest

from judge.gpu_environment import ARCHIVE_BYTES, PrivateRuntime, copy_archive
from judge.sandbox import SandboxUnavailable


def test_staged_archive_is_an_independent_bounded_snapshot(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "private"
    source.write_bytes(b"fixed artifact")
    calls = []
    copy_archive(source, destination, lambda: calls.append(True))
    source.write_bytes(b"replacement")
    assert destination.read_bytes() == b"fixed artifact" and calls
    with pytest.raises(FileExistsError):
        copy_archive(source, destination, lambda: None)


@pytest.mark.parametrize("kind", ["fifo", "link", "large", "empty"])
def test_invalid_artifacts_are_rejected_before_copying(tmp_path, kind):
    source, destination = tmp_path / "source", tmp_path / "private"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "link":
        original = tmp_path / "original"
        original.write_bytes(b"artifact")
        source.symlink_to(original)
    else:
        with source.open("wb") as stream:
            stream.truncate(ARCHIVE_BYTES + 1 if kind == "large" else 0)
    with pytest.raises((SandboxUnavailable, OSError)):
        copy_archive(source, destination, lambda: None)
    assert not destination.exists()


def test_cpu_preparation_cannot_create_a_cuda_session():
    runtime = PrivateRuntime.__new__(PrivateRuntime)
    runtime.target = None
    with pytest.raises(SandboxUnavailable, match="explicit GPU and compiler"):
        runtime.kernel_session("not-authorized", mode="run")


def inspection(tmp_path):
    return {
        "Name": "owned",
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "OCIRuntime": str(tmp_path / "runtime/runsc"),
        "Config": {"User": "65534:65534"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "none",
            "IpcMode": "none",
            "Memory": 512 * 1024**2,
            "MemorySwap": 512 * 1024**2,
            "PidsLimit": 256,
            "CpuQuota": 50000,
            "CpuPeriod": 100000,
            "SecurityOpt": ["no-new-privileges"],
            "LogConfig": {"Type": "none"},
        },
    }


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "Id", "foreign-container"),
        (None, "Image", "sha256:" + "c" * 64),
        (None, "OCIRuntime", "/usr/bin/runc"),
        ("Config", "User", "0:0"),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "Privileged", True),
        ("HostConfig", "NetworkMode", "host"),
        ("HostConfig", "IpcMode", "host"),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "MemorySwap", -1),
        ("HostConfig", "PidsLimit", -1),
        ("HostConfig", "CpuQuota", -1),
        ("HostConfig", "SecurityOpt", []),
        ("HostConfig", "LogConfig", {"Type": "journald"}),
    ],
)
def test_changed_container_or_isolation_configuration_is_rejected(tmp_path, section, key, value):
    runtime = PrivateRuntime.__new__(PrivateRuntime)
    runtime.base = tmp_path
    runtime.identity = {"image_id": "sha256:" + "b" * 64}
    runtime.containers = {"task": {"name": "owned", "id": "a" * 64}}
    details = inspection(tmp_path)
    runtime.inspect_json = lambda *args, **kwargs: [deepcopy(details)]
    assert runtime.inspect("task")["Id"] == "a" * 64
    (details if section is None else details[section])[key] = value
    with pytest.raises(SandboxUnavailable, match="identity or isolation"):
        runtime.inspect("task")
