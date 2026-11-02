"""Local GPU ownership on the original physical lock and prepared installation.

This stores identities, never readiness. Recovery only removes a matching original
container; it neither runs the old image nor confirms an application reservation.
"""

import hashlib
import os
import re
import uuid
from copy import deepcopy

from judge.container_contract import PYTHON, WORKLOAD_PROFILE
from judge.container_devices import encoded_selection
from judge.container_environment import validate_environment
from judge.container_journal import ContainerJournal, private
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

MARKER = "local-practice.json"
OPERATION = "local-operation.json"
SCOPE = "praximesh-local-gpu-state-v1"
KERNEL = [PYTHON, "-I", "/opt/praximesh/container_kernel_entry.py"]


def require(value, message):
    if not value:
        raise SandboxUnavailable(message)


def inode(info):
    return [info.st_dev, info.st_ino]


def reject_local_state(directory_fd):
    """Worker configuration reads must reject even an invalid local marker."""
    try:
        fd = os.open(
            "journal",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return
    try:
        for name in (MARKER, OPERATION):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("Local GPU practice state cannot become a server worker")
    finally:
        os.close(fd)


class LocalPracticeState:
    def __init__(self, journal, *, create=False):
        if type(journal) is not ContainerJournal or type(create) is not bool:
            raise ValueError("The original physical journal is required")
        self.journal, self.guard = journal, journal.guard
        self.root = journal.directory.parent
        self.fd = None
        try:
            self.fd = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            binding = self.binding()
            marker = journal.read(MARKER)
            if marker is None:
                require(create, "Explicit local initialization is required")
                require(
                    set(os.listdir(journal.fd)) == {"installation.json", "publication.lock"},
                    "Local initialization requires unused original state",
                )
                marker = {"schema": 1, "scope": SCOPE, "id": uuid.uuid4().hex, "binding": binding}
                journal.write(MARKER, marker, create=True)
            require(
                type(marker) is dict
                and set(marker) == {"schema", "scope", "id", "binding"}
                and type(marker["schema"]) is int
                and marker["schema"] == 1
                and marker["scope"] == SCOPE
                and type(marker["id"]) is str
                and re.fullmatch(r"[a-f0-9]{32}", marker["id"])
                and marker["binding"] == binding,
                "Local practice belongs to a different original installation",
            )
            self.marker = deepcopy(marker)
            self.check()
        except BaseException:
            self.close()
            raise

    def binding(self):
        self.guard.assert_held()
        self.journal.check_engine()
        private(os.fstat(self.fd), directory=True)
        require(
            inode(os.fstat(self.fd)) == inode(self.root.lstat())
            and self.root.resolve() == self.root
            and self.journal.directory == self.root / "journal"
            and self.guard.directory == self.root / "locks"
            and set(os.listdir(self.fd)) == {"mounts.json", "locks", "journal"},
            "Local practice requires its original standalone state, without worker or deployment data",
        )
        fd = os.open(
            "mounts.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=self.fd,
        )
        with os.fdopen(fd, "rb") as stream:
            private(os.fstat(stream.fileno()))
            raw = stream.read(8193)
        require(len(raw) <= 8192, "Original controller mounts exceed their bound")
        mounts = decode_frame(raw)
        require(
            type(mounts) is dict
            and type(mounts.get("version")) is int
            and mounts["version"] == 4
            and mounts.get("root") == inode(os.fstat(self.fd))
            and mounts.get("locks") == inode(self.guard.directory.lstat())
            and mounts.get("journal") == inode(os.fstat(self.journal.fd))
            and mounts.get("engine") == self.journal.identity["engine"]
            and mounts.get("device_observer", {}).get("target", {}).get("uuid")
            == self.guard.device_uuid,
            "Local practice requires the original v4 physical device identity",
        )
        return {
            "device": self.guard.device_uuid,
            "root": inode(os.fstat(self.fd)),
            "lock": inode((self.guard.directory / self.guard.filename).lstat()),
            "mounts_sha256": hashlib.sha256(raw).hexdigest(),
            "installation": deepcopy(self.journal.identity),
        }

    def check(self):
        require(
            self.binding() == self.marker["binding"] and self.journal.read(MARKER) == self.marker,
            "Local practice ownership changed; retain original state",
        )

    def operation(self):
        self.check()
        value = self.journal.read(OPERATION)
        if value is None:
            return None
        try:
            valid = (
                type(value) is dict
                and set(value)
                == {"schema", "scope", "binding", "job", "reservation", "phase", "environment"}
                and type(value["schema"]) is int
                and value["schema"] == 1
                and value["scope"] == SCOPE
                and value["binding"] == canonical_digest(self.marker)
                and value["phase"] in {"pending", "completed", "recovered"}
                and all(
                    type(value[key]) is str and str(uuid.UUID(value[key])) == value[key]
                    for key in ("job", "reservation")
                )
                and value["environment"]["target"]["uuid"] == self.guard.device_uuid
            )
            validate_environment(value["environment"], canonical_digest(value["environment"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        require(valid, "Invalid original local operation; retain its record")
        return value

    def begin(self, environment):
        self.check()
        validate_environment(environment, canonical_digest(environment))
        old = self.operation()
        require(
            self.journal.load() is None and (old is None or old["phase"] != "pending"),
            "Recover the pending local operation before starting another",
        )
        require(
            environment["target"]["uuid"] == self.guard.device_uuid,
            "Local operation selected another device",
        )
        value = {
            "schema": 1,
            "scope": SCOPE,
            "binding": canonical_digest(self.marker),
            "job": str(uuid.uuid4()),
            "reservation": str(uuid.uuid4()),
            "phase": "pending",
            "environment": deepcopy(environment),
        }
        self.journal.write(OPERATION, value)
        self.assert_operation(value)
        return deepcopy(value)

    def assert_operation(self, value):
        require(self.operation() == value, "Original local operation changed")

    def finish(self, value, *, recovered=False):
        self.assert_operation(value)
        require(
            value["phase"] == "pending" and self.journal.load() is None,
            "Original local task is still pending",
        )
        saved = {**value, "phase": "recovered" if recovered else "completed"}
        self.journal.write(OPERATION, saved)
        self.assert_operation(saved)
        return deepcopy(saved)

    def recover(self):
        value, record = self.operation(), self.journal.load()
        if value is None or value["phase"] != "pending":
            require(record is None, "No local operation owns the original container")
            return {"status": "idle", "ready": False, "formal_result": False}
        if record is not None:
            environment = value["environment"]
            require(
                record["version"] == 3
                and record["resource_profile"] == WORKLOAD_PROFILE
                and record["purpose"] in {"maintenance", "task"}
                and record["job"] == value["job"]
                and record["reservation"] == value["reservation"]
                and record["image"] == environment["artifacts"]["task_image"]
                and record["device_spec"] == environment["device_spec"]
                and record["arguments"][4:] == [*KERNEL, encoded_selection(record["device_spec"])],
                "The pending container does not belong to this local operation",
            )
        cleanup = self.journal.recover() if record is not None else None
        self.finish(value, recovered=True)
        return {"status": "recovered", "ready": False, "formal_result": False, "cleanup": cleanup}

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()
