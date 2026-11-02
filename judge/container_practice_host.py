"""Private host-command serialization on an existing local physical installation.

The client lock never grants device ownership. A controller must still take the
original physical lock; the host releases it before starting that controller.
"""

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from judge.container_controller import validate_mounts
from judge.container_journal import MAX_RECORD, NONCE, ContainerJournal, private
from judge.container_practice_state import (
    MARKER,
    LocalPracticeState,
    inode,
    require,
    validate_marker,
)
from judge.container_processes import host_manifest
from judge.container_service import LocalEngine
from judge.gpu_device_lock import DeviceLock
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

SCOPE = "praximesh-local-controller-host-v1"
CLIENT_LOCK = "local-client.lock"
RECORD = "local-controller.json"


class HostWorkspace:
    def __init__(self, directory, engine, *, initialize=False):
        self.root, self.engine = Path(directory), engine
        self.stack = ExitStack()
        self.client_fd = self.pid = None
        self.fds = {}
        if (
            type(engine) is not LocalEngine
            or type(initialize) is not bool
            or os.getuid() == 0
            or os.getgid() == 0
            or not self.root.is_absolute()
            or ".." in self.root.parts
            or self.root.resolve() != self.root
            or any(c in str(self.root) for c in ",\n\r\0")
            or engine.path.resolve() != engine.path
            or any(c in str(engine.path) for c in ",\n\r\0")
        ):
            raise ValueError(
                "Use the original absolute private state and rootless socket as non-root"
            )
        try:
            for name, path in (
                ("root", self.root),
                ("locks", self.root / "locks"),
                ("journal", self.root / "journal"),
            ):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                self.fds[name] = fd
                self.stack.callback(os.close, fd)
                private(os.fstat(fd), directory=True)
            self.mount_bytes = self.read_bytes("mounts.json", directory="root", maximum=8192)
            require(self.mount_bytes is not None, "Original controller mounts are missing")
            mounts = decode_frame(self.mount_bytes)
            require(
                type(mounts) is dict and mounts.get("version") == 4,
                "Original v4 physical state is required",
            )
            self.device = mounts.get("device_observer", {}).get("target", {}).get("uuid")
            self.mounts = deepcopy(validate_mounts(mounts, self.device))
            require(
                self.device == self.device.lower().replace("gpu-", "GPU-", 1),
                "Original device UUID must be canonical",
            )
            self.installation = self.read("installation.json")
            require(
                type(self.installation) is dict
                and set(self.installation)
                == {"version", "installation", "engine", "lock_namespace"}
                and type(self.installation["version"]) is int
                and self.installation["version"] == 1
                and type(self.installation["installation"]) is str
                and NONCE.fullmatch(self.installation["installation"])
                and self.installation["engine"] == self.mounts["engine"]
                and self.installation["lock_namespace"] == self.mounts["locks"],
                "The original installation is missing or inconsistent",
            )
            self.binding = self.physical_binding()
            marker = self.read(MARKER)
            client = self.read(CLIENT_LOCK)
            if marker is None or client is None:
                require(
                    marker is not None or initialize, "Explicit local initialization is required"
                )
                require(
                    client is None and self.read(RECORD) is None,
                    "Do not replace original client ownership",
                )
                with DeviceLock(self.root / "locks", self.device) as guard:
                    with ContainerJournal(self.root / "journal", guard, engine) as journal:
                        with LocalPracticeState(journal, create=initialize) as state:
                            marker = deepcopy(state.marker)
                            client = {
                                "schema": 1,
                                "scope": SCOPE,
                                "nonce": uuid.uuid4().hex,
                                "binding": canonical_digest(marker),
                            }
                            journal.write(CLIENT_LOCK, client, create=True)
            self.marker = deepcopy(validate_marker(marker, self.binding))
            require(
                type(client) is dict
                and set(client) == {"schema", "scope", "nonce", "binding"}
                and type(client["schema"]) is int
                and client["schema"] == 1
                and client["scope"] == SCOPE
                and type(client["nonce"]) is str
                and NONCE.fullmatch(client["nonce"])
                and client["binding"] == canonical_digest(self.marker),
                "Invalid original local client lock",
            )
            self.client = deepcopy(client)
            self.client_fd = os.open(
                CLIENT_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fds["journal"]
            )
            self.stack.callback(os.close, self.client_fd)
            private(os.fstat(self.client_fd))
            try:
                fcntl.flock(self.client_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SandboxUnavailable("Another local command owns this installation") from None
            self.pid, self.client_inode = os.getpid(), inode(os.fstat(self.client_fd))
            self.anchor = {
                "scope": SCOPE,
                "binding": canonical_digest(self.marker),
                "client": self.client,
                "lock": self.client_inode,
            }
            self.record = self.read(RECORD)
            require(
                self.record is None
                or (
                    type(self.record) is dict
                    and canonical_digest(self.record.get("anchor")) == canonical_digest(self.anchor)
                ),
                "Original controller record belongs to a different client lock",
            )
            self.check()
        except BaseException:
            self.close()
            raise

    def read_bytes(self, name, *, directory="journal", maximum=MAX_RECORD):
        require(
            type(name) is str
            and name not in {"", ".", ".."}
            and "/" not in name
            and "\0" not in name,
            "Expected a private state basename",
        )
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.fds[directory],
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            private(os.fstat(stream.fileno()))
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "Private local state exceeded its bound")
        return raw

    def read(self, name):
        raw = self.read_bytes(name)
        return None if raw is None else decode_frame(raw)

    def physical_binding(self):
        for name, fd in self.fds.items():
            path = self.root if name == "root" else self.root / name
            opened, named = os.fstat(fd), path.lstat()
            private(opened, directory=True)
            private(named, directory=True)
            require(
                inode(opened) == inode(named) == self.mounts[name],
                "Original local directory changed",
            )
        self.engine.check_socket()
        require(
            set(os.listdir(self.fds["root"])) == {"mounts.json", "locks", "journal"}
            and self.mounts["operator"] == [os.getuid(), os.getgid()]
            and inode(self.engine.path.lstat()) == self.mounts["socket"]
            and self.mount_bytes == self.read_bytes("mounts.json", directory="root", maximum=8192)
            and canonical_digest(self.read("installation.json"))
            == canonical_digest(self.installation)
            and canonical_digest(host_manifest()) == canonical_digest(self.mounts["host_processes"])
            and self.engine.verify()["ID"] == self.mounts["engine"],
            "Original local mounts, operator or daemon changed",
        )
        lock = (self.root / "locks" / (self.device + ".lock")).lstat()
        private(lock)
        return {
            "device": self.device,
            "root": self.mounts["root"],
            "lock": inode(lock),
            "mounts_sha256": hashlib.sha256(self.mount_bytes).hexdigest(),
            "installation": deepcopy(self.installation),
        }

    def check(self):
        require(
            self.client_fd is not None and self.pid == os.getpid(),
            "Original local client lock is not held",
        )
        opened = os.fstat(self.client_fd)
        named = os.stat(CLIENT_LOCK, dir_fd=self.fds["journal"], follow_symlinks=False)
        private(opened)
        private(named)
        require(
            inode(opened) == inode(named) == self.client_inode
            and canonical_digest(self.read(CLIENT_LOCK)) == canonical_digest(self.client)
            and canonical_digest(self.physical_binding()) == canonical_digest(self.binding)
            and canonical_digest(self.read(MARKER)) == canonical_digest(self.marker),
            "Original local command ownership changed; retain state",
        )

    @contextmanager
    def physical(self):
        self.check()
        with DeviceLock(self.root / "locks", self.device) as guard:
            with ContainerJournal(self.root / "journal", guard, self.engine) as journal:
                with LocalPracticeState(journal) as state:
                    self.check()
                    require(
                        canonical_digest(state.marker) == canonical_digest(self.marker),
                        "Original physical binding changed",
                    )
                    yield state
                    self.check()

    def write_record(self, value):
        self.check()
        require(
            type(value) is dict
            and canonical_digest(value.get("anchor")) == canonical_digest(self.anchor)
            and canonical_digest(self.read(RECORD)) == canonical_digest(self.record),
            "Original controller record changed; retain state",
        )
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        require(len(encoded) <= MAX_RECORD, "Local controller record exceeded its bound")
        temporary = ".local-controller-write-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fds["journal"],
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self.check()
            require(
                canonical_digest(self.read(RECORD)) == canonical_digest(self.record),
                "Original controller record changed before publication",
            )
            os.replace(
                temporary, RECORD, src_dir_fd=self.fds["journal"], dst_dir_fd=self.fds["journal"]
            )
            os.fsync(self.fds["journal"])
            self.record = deepcopy(value)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fds["journal"])
            except FileNotFoundError:
                pass
        self.check()
        require(
            canonical_digest(self.read(RECORD)) == canonical_digest(value),
            "Local controller record changed during publication",
        )

    def close(self):
        self.stack.close()
        self.client_fd = self.pid = None

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()
