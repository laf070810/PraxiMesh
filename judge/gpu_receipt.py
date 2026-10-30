"""Private, current-owner GPU startup receipts; no probe or import shortcut."""

import copy
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from judge.gpu_execution_contract import canonical_digest, environment_identity
from judge.gpu_kernel_checks import CHECKS as KERNEL_CHECKS
from judge.gpu_lifecycle import CHECKS as CONTROLLER_CHECKS
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

TTL_SECONDS = 86400
CHECK_SECONDS = 180
CHECKS = (
    "initial-recovery",
    "resources",
    *("kernel/" + name for name in KERNEL_CHECKS),
    *("controller/" + name for name in CONTROLLER_CHECKS),
)
SUITE_SECONDS = CHECK_SECONDS * len(CHECKS)
MAX_BYTES = 64 * 1024


def require(condition, message):
    if not condition:
        raise SandboxUnavailable(message)


def host_identity():
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    require(str(uuid.UUID(boot)) == boot, "Invalid host boot identity")
    return {"boot_id": boot, "uid": os.getuid(), "kernel": os.uname().release}


def binding(guard, worker_id, adapter):
    if type(guard) is not DeviceLock or type(adapter) is not GPUExecutionAdapter:
        raise ValueError("GPU readiness requires an owned lock and supervised adapter")
    guard.assert_held()
    if type(worker_id) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", worker_id):
        raise ValueError("An explicit GPU worker identity is required")
    require(
        guard.device_uuid == adapter.target["uuid"]
        and environment_identity(adapter.target, adapter.identity) == adapter.environment
        and canonical_digest(adapter.environment) == adapter.fingerprint,
        "GPU startup environment changed",
    )
    return {
        "schema": 1,
        "suite": canonical_digest(
            {"id": "gpu-startup-v1", "checks": CHECKS, "check_seconds": CHECK_SECONDS}
        ),
        "checks": list(CHECKS),
        "environment": copy.deepcopy(adapter.environment),
        "fingerprint": adapter.fingerprint,
        "device": guard.device_uuid,
        "worker": worker_id,
        "owner_token": str(guard.owner_token),
        "host": host_identity(),
        "valid_for_seconds": TTL_SECONDS,
    }


def validate_receipt(value, guard, worker_id, adapter, *, minimum_remaining=CHECK_SECONDS):
    """A receipt is valid only while the same process still holds its host lock."""
    if (
        type(minimum_remaining) not in {int, float}
        or not math.isfinite(minimum_remaining)
        or not 0 <= minimum_remaining < TTL_SECONDS
    ):
        raise ValueError("Invalid GPU receipt validity margin")
    expected = binding(guard, worker_id, adapter)
    require(
        type(value) is dict
        and set(value) == set(expected) | {"created_wall", "created_monotonic", "evidence_digest"},
        "Incomplete GPU startup receipt",
    )
    try:
        matches = canonical_digest({name: value[name] for name in expected}) == canonical_digest(
            expected
        )
    except (ValueError, TypeError):
        matches = False
    require(matches, "GPU receipt does not match the current owner, host or complete suite")
    require(
        type(value["evidence_digest"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", value["evidence_digest"]),
        "GPU startup evidence digest is missing",
    )
    for key, now in (("created_wall", time.time()), ("created_monotonic", time.monotonic())):
        created = value[key]
        require(
            type(created) in {int, float}
            and math.isfinite(created)
            and created >= 0
            and 0 <= now - created < TTL_SECONDS - minimum_remaining,
            "GPU startup receipt expired or its clock changed",
        )
    guard.assert_held()
    return copy.deepcopy(value)


class ReceiptStore:
    """No symlinks, shared files or silent overwrite; all operations stay owned."""

    def __init__(self, directory, guard, *, create=False):
        if type(guard) is not DeviceLock:
            raise ValueError("A held GPU device lock is required")
        guard.assert_held()
        self.directory = Path(directory)
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("An absolute private GPU receipt directory is required")
        self.guard, self.create = guard, create
        self.filename = guard.device_uuid + ".json"
        self.fd = None

    def __enter__(self):
        if self.create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            self._check_directory()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_error):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _check_directory(self):
        self.guard.assert_held()
        require(self.fd is not None, "GPU receipt directory is not open")
        opened = os.fstat(self.fd)
        named = os.stat(self.directory, follow_symlinks=False)
        require(
            stat.S_ISDIR(named.st_mode)
            and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
            and opened.st_uid == os.getuid()
            and stat.S_IMODE(opened.st_mode) == 0o700,
            "GPU receipt directory must remain private and unchanged",
        )

    def _file(self, flags):
        self._check_directory()
        fd = os.open(
            self.filename, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.fd
        )
        try:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1,
                "GPU receipt must be an owned private single-link regular file",
            )
        except BaseException:
            os.close(fd)
            raise
        return fd, info

    def invalidate(self):
        try:
            fd, info = self._file(os.O_RDONLY)
        except FileNotFoundError:
            self._check_directory()
            return
        try:
            named = os.stat(self.filename, dir_fd=self.fd, follow_symlinks=False)
            require(
                (info.st_dev, info.st_ino) == (named.st_dev, named.st_ino),
                "GPU receipt changed before invalidation",
            )
            self._check_directory()
            os.unlink(self.filename, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)

    def read(self):
        fd, before = self._file(os.O_RDONLY)
        try:
            require(before.st_size <= MAX_BYTES, "GPU receipt exceeds its size limit")
            chunks = []
            remaining = MAX_BYTES + 1
            while remaining:
                data = os.read(fd, min(remaining, 8192))
                if not data:
                    break
                chunks.append(data)
                remaining -= len(data)
            after = os.fstat(fd)
            named = os.stat(self.filename, dir_fd=self.fd, follow_symlinks=False)
            self._check_directory()
            require(
                remaining > 0
                and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and (before.st_dev, before.st_ino) == (named.st_dev, named.st_ino)
                and after.st_nlink == 1,
                "GPU receipt changed during verification",
            )
            return decode_frame(b"".join(chunks))
        finally:
            os.close(fd)

    def _write(self, encoded):
        """Only the complete startup orchestrator publishes a new receipt."""
        self._check_directory()
        require(
            type(encoded) is bytes and 0 < len(encoded) <= MAX_BYTES, "Invalid GPU receipt size"
        )
        temporary = "." + self.filename + "-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            os.fchmod(fd, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                require(written > 0, "GPU receipt write made no progress")
                offset += written
            os.fsync(fd)
            self._check_directory()
            # Atomic publication without replacing any unexpectedly existing
            # file. Remove the temporary link before any readiness transition.
            os.link(
                temporary,
                self.filename,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        finally:
            os.close(fd)
            os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)


def check_receipt(directory, guard, worker_id, adapter, *, minimum_remaining=CHECK_SECONDS):
    with ReceiptStore(directory, guard) as store:
        value = store.read()
    return validate_receipt(value, guard, worker_id, adapter, minimum_remaining=minimum_remaining)
