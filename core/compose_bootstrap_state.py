"""Original host installation fence, retained independently of PostgreSQL volumes."""

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from judge.container_journal import private
from judge.container_preparation import create_json
from scripts.deploy_database import DeploymentDatabaseError

STATE = Path("/run/praximesh-bootstrap")


def initialize(directory, digest):
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory or directory.exists():
        raise ValueError("Bootstrap identity requires a new absolute private directory")
    directory.mkdir(mode=0o700)
    info = directory.stat()
    create_json(
        directory / "identity.json",
        {
            "schema": 1,
            "configuration": digest,
            "device": info.st_dev,
            "inode": info.st_ino,
        },
    )
    create_json(directory / "state.json", {"schema": 1, "bound": False})
    descriptor = os.open(directory / "bootstrap.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class InstallationState:
    def __init__(self, directory, digest):
        self.directory, self.digest = Path(directory), digest
        self.fd = self.lock_fd = None

    def read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            private(os.fstat(stream.fileno()))
            data = stream.read(4097)
            if len(data) > 4096:
                raise DeploymentDatabaseError("Bootstrap identity exceeds its bound")
            return json.loads(data)

    def check(self):
        if self.fd is None:
            raise DeploymentDatabaseError("Original bootstrap file lock is required")
        opened, named = os.fstat(self.fd), self.directory.lstat()
        private(opened, directory=True)
        private(named, directory=True)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise DeploymentDatabaseError("Bootstrap installation directory changed")
        locked = os.fstat(self.lock_fd)
        named_lock = os.stat("bootstrap.lock", dir_fd=self.fd, follow_symlinks=False)
        private(named_lock)
        if (locked.st_dev, locked.st_ino) != (named_lock.st_dev, named_lock.st_ino):
            raise DeploymentDatabaseError("Original bootstrap file lock changed")
        identity = self.read("identity.json")
        if (
            type(identity) is not dict
            or type(identity.get("schema")) is not int
            or identity
            != {
                "schema": 1,
                "configuration": self.digest,
                "device": opened.st_dev,
                "inode": opened.st_ino,
            }
        ):
            raise DeploymentDatabaseError("Original bootstrap configuration or directory changed")
        state = self.read("state.json")
        if (
            type(state) is not dict
            or set(state) != {"schema", "bound"}
            or type(state["schema"]) is not int
            or state["schema"] != 1
            or type(state["bound"]) is not bool
        ):
            raise DeploymentDatabaseError("Invalid original bootstrap binding state")
        return state["bound"]

    @contextmanager
    def locked(self):
        if self.fd is not None:
            raise DeploymentDatabaseError("Nested bootstrap locking is forbidden")
        if not self.directory.is_absolute() or self.directory.resolve() != self.directory:
            raise DeploymentDatabaseError("Original absolute bootstrap directory is required")
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            private(os.fstat(self.fd), directory=True)
            lock = os.open(
                "bootstrap.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd
            )
            private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = lock
            self.check()
            yield self
            self.check()
        finally:
            if lock is not None:
                os.close(lock)
            os.close(self.fd)
            self.fd = self.lock_fd = None

    def bind(self):
        if self.check():
            return
        temporary = ".binding-" + uuid.uuid4().hex
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"schema": 1, "bound": True}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.check()
        os.replace(temporary, "state.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        if not self.check():
            raise DeploymentDatabaseError("Bootstrap database binding was not persisted")
