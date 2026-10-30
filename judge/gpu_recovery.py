"""Recover only stopped, owned runtime files; never run GPU work or release a lease."""

import hashlib
import json
import math
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from judge.gpu_namespace import RUNTIME_OWNER, JobWorkspace, mounted_paths, private_directory
from judge.gpu_service import OwnedJobService
from judge.gpu_transport import PrivateListener
from judge.sandbox import SandboxUnavailable

STORE_DIRECTORIES = ("data", "run", "tmp")
RECOVERY_SECONDS = 60


def private_record(fd, name):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 0 < info.st_size <= 16384
        ):
            raise SandboxUnavailable("Runtime recovery record is not private")
        value = stream.read(16385)
        if len(value) != info.st_size:
            raise SandboxUnavailable("Runtime recovery record changed")
        return value


def namespace_mapping():
    if os.getuid() != 0 or Path("/proc/self/cgroup").read_text().strip() != "0::/manager":
        raise SandboxUnavailable("Runtime recovery requires the private controller namespace")
    mappings = {}
    for name in ("uid", "gid"):
        rows = [
            list(map(int, line.split()))
            for line in Path(f"/proc/self/{name}_map").read_text().splitlines()
        ]
        if (
            not rows
            or any(len(row) != 3 or min(row) < 0 or row[2] <= 0 for row in rows)
            or rows[0][0] != 0
            or rows[0][1] == 0
            or rows[0][2] != 1
            or not any(start <= 65534 < start + count for start, _outside, count in rows)
        ):
            raise SandboxUnavailable("Runtime recovery requires the original subordinate IDs")
        mappings[name] = rows
    return mappings


def record_runtime_owner(base):
    """Persist before creating any mapped files, without source or container PIDs."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = private_directory(fd)
        record = {
            "version": 1,
            "device": info.st_dev,
            "inode": info.st_ino,
            "identity_sha256": hashlib.sha256(private_record(fd, "identity.json")).hexdigest(),
            "mapping": namespace_mapping(),
        }
        opened = os.open(
            RUNTIME_OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        with os.fdopen(opened, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)


def cleanup_request(workspace):
    workspace.assert_stopped()
    info = private_directory(workspace._fd)
    return {
        "path": str(workspace.path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "identity_sha256": hashlib.sha256(
            private_record(workspace._fd, "identity.json")
        ).hexdigest(),
        "owner_sha256": hashlib.sha256(private_record(workspace._fd, RUNTIME_OWNER)).hexdigest(),
    }


def remove_runtime_files(request, on_tick):
    """Fixed helper operation after outer peer authorization and full service stop.

    Re-enter only the same UID map, never the old mount/PID namespaces. Opening
    a stale container database could act on recycled host PIDs, so no runtime or
    executable from the abandoned workspace is invoked here.
    """
    if type(request) is not dict or set(request) != {
        "path",
        "device",
        "inode",
        "identity_sha256",
        "owner_sha256",
    }:
        raise SandboxUnavailable("Invalid runtime recovery request")
    base = Path(request["path"])
    if not base.is_absolute() or ".." in base.parts or base.resolve() != base:
        raise SandboxUnavailable("Recovery requires an absolute owned directory")
    mapping = namespace_mapping()
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = private_directory(fd)

        def check():
            on_tick()
            current = base.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise SandboxUnavailable("Runtime recovery directory was replaced")
            if (info.st_dev, info.st_ino) != (request["device"], request["inode"]):
                raise SandboxUnavailable("Runtime recovery directory identity changed")
            identity = private_record(fd, "identity.json")
            owner = private_record(fd, RUNTIME_OWNER)
            if (
                hashlib.sha256(identity).hexdigest() != request["identity_sha256"]
                or hashlib.sha256(owner).hexdigest() != request["owner_sha256"]
            ):
                raise SandboxUnavailable("Runtime recovery belongs to another reservation")
            expected = {
                "version": 1,
                "device": info.st_dev,
                "inode": info.st_ino,
                "identity_sha256": request["identity_sha256"],
                "mapping": mapping,
            }
            if owner != json.dumps(expected, sort_keys=True).encode():
                raise SandboxUnavailable("Runtime recovery mapping or identity changed")
            if any(path.is_relative_to(base) for path in mounted_paths()):
                raise SandboxUnavailable("Runtime recovery refuses mounted content")

        check()
        if not shutil.rmtree.avoids_symlink_attacks:
            raise SandboxUnavailable("Descriptor-based runtime cleanup is required")
        # Preflight all roots before deleting any of them. Nested symlinks are
        # unlinked by the descriptor-based implementation, never traversed.
        for name in STORE_DIRECTORIES:
            try:
                child = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(child.st_mode):
                raise SandboxUnavailable("Runtime recovery store is not a directory")
        removed = []
        for name in STORE_DIRECTORIES:
            check()
            try:
                shutil.rmtree(name, dir_fd=fd)
            except FileNotFoundError:
                # A partial previous recovery may already have removed it.
                if name in os.listdir(fd):
                    raise
            removed.append(name)
        check()
        return removed
    finally:
        os.close(fd)


def recover_workspace(workspace, *, deadline, on_tick):
    """Bounded filesystem recovery under the held lock; caller keeps reservation.

    The task service must already be stopped. This function does not observe GPU
    state, call the database, run a learner task, or authorize further work.
    """
    if type(workspace) is not JobWorkspace or workspace.service.purpose != "task":
        raise ValueError("An owned task workspace is required")
    if type(deadline) not in {int, float} or not math.isfinite(deadline):
        raise SandboxUnavailable("A finite private recovery deadline is required")
    workspace.assert_stopped()
    deadline = min(deadline, time.monotonic() + RECOVERY_SECONDS)
    if deadline - time.monotonic() <= 12:
        raise SandboxUnavailable("Insufficient time for private storage recovery")
    original = workspace.service
    service = OwnedJobService(
        original.guard, original.job_id, original.reservation_token, purpose="recovery"
    )
    # Recover an interrupted cleanup before replacing its environment. A fresh
    # service object can adopt only this reservation's independently named unit.
    service.stop()
    recovery_name = service.unit.removeprefix("praximesh-gpu-").removesuffix(".service")
    if os.path.lexists(workspace.directory / recovery_name):
        with JobWorkspace(workspace.directory, service, create=False) as previous:
            previous.remove_after_stop()
    service = OwnedJobService(
        original.guard, original.job_id, original.reservation_token, purpose="recovery"
    )
    request = cleanup_request(workspace)
    with JobWorkspace(workspace.directory, service) as recovery, PrivateListener() as listener:
        channel = None

        def tick():
            workspace.assert_stopped()
            on_tick()

        try:
            entry = Path(__file__).with_name("gpu_recovery_entry.py")
            service.start(
                recovery.command(
                    [sys.executable, "-I", str(entry), str(listener.path), str(deadline - 12)]
                ),
                deadline=deadline,
                on_tick=tick,
            )
            channel = listener.accept(service, deadline=deadline - 10, on_tick=tick)
            if channel.receive() != {"event": "ready"}:
                raise SandboxUnavailable("Private storage recovery did not become ready")
            tick()
            channel.send(request)
            if channel.receive() != {"event": "removed", "directories": list(STORE_DIRECTORIES)}:
                raise SandboxUnavailable("Private storage recovery did not finish")
            channel.send({"event": "finish"})
            channel.expect_eof()
        finally:
            if channel is not None:
                channel.close()
            stopped = service.stop()
            recovery.remove_after_stop()
    workspace.assert_stopped()
    workspace.remove_after_stop()
    return {"unit": service.unit, "service_cleanup": stopped, "workspace_removed": True}
