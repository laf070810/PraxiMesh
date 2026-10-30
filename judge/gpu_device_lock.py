"""Exclusive per-device host ownership; never removes an existing lock inode."""

import fcntl
import os
import re
import stat
import uuid
from pathlib import Path

GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class DeviceLockError(RuntimeError):
    pass


class DeviceLock:
    def __init__(self, directory, device_uuid):
        if not isinstance(device_uuid, str) or not GPU_UUID.fullmatch(device_uuid):
            raise DeviceLockError("A full GPU UUID is required")
        self.directory = Path(directory)
        self.device_uuid = device_uuid.lower().replace("gpu-", "GPU-", 1)
        self.owner_token = uuid.uuid4()
        self._fd = None
        self._directory_fd = None
        self._pid = None

    @property
    def filename(self):
        return self.device_uuid + ".lock"

    def acquire(self):
        if self._fd is not None:
            raise DeviceLockError("Device lock is already held by this object")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        try:
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise DeviceLockError("GPU lock directory must be private and owned by this user")
            fd = os.open(
                self.filename,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise DeviceLockError("GPU lock must be an owned private regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeviceLockError("Another controller owns this GPU") from None
            self.owner_token = uuid.uuid4()
            self._fd, self._directory_fd, self._pid = fd, directory_fd, os.getpid()
            self.assert_held()
            return self
        except BaseException:
            if fd is not None:
                os.close(fd)
            os.close(directory_fd)
            self._fd = self._directory_fd = self._pid = None
            raise

    def assert_held(self):
        if self._fd is None or self._pid != os.getpid():
            raise DeviceLockError("GPU ownership lock is not held by this process")
        opened = os.fstat(self._fd)
        named = os.stat(self.filename, dir_fd=self._directory_fd, follow_symlinks=False)
        directory = os.stat(self.directory, follow_symlinks=False)
        opened_directory = os.fstat(self._directory_fd)
        if (
            (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_nlink != 1
            or (directory.st_dev, directory.st_ino)
            != (opened_directory.st_dev, opened_directory.st_ino)
        ):
            raise DeviceLockError("GPU ownership lock was replaced")

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            os.close(self._directory_fd)
            self._fd = self._directory_fd = self._pid = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_error):
        self.close()
