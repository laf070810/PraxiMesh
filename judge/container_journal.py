"""Private durable ownership with frozen legacy and explicit task budgets."""

import fcntl
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from judge.container_contract import (
    MAX_SECONDS,
    PROTOCOL,
    SUPERVISION_PROFILE,
    WORKLOAD_PROFILE,
    resource_marker,
)
from judge.container_devices import docker_devices, validate_selection
from judge.container_runtime_service import RuntimeContainerService, seccomp_profile
from judge.container_service import HEX_ID, IMAGE_ID, PREFIX, ContainerService, digest
from judge.gpu_device_lock import DeviceLock
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

SCOPE = "praximesh-persistent-runsc-container-v1"
MAX_RECORD = 128 * 1024
NONCE = re.compile("[a-f0-9]{32}")
KEYS = {
    "version",
    "installation",
    "engine",
    "lock_namespace",
    "device",
    "job",
    "reservation",
    "purpose",
    "owner",
    "name",
    "image",
    "labels",
    "security",
    "image_env",
    "arguments",
    "container_id",
    "started_at",
    "phase",
}


def owner(identity, device, job, reservation, purpose):
    return digest(
        [
            SCOPE,
            identity["installation"],
            identity["engine"],
            identity["lock_namespace"],
            device,
            str(job),
            str(reservation),
            purpose,
        ]
    )


def private(info, *, directory=False):
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and (info.st_nlink != 1 or info.st_size > MAX_RECORD))
    ):
        raise SandboxUnavailable("Container journal must be private and owned")


class ContainerJournal:
    def __init__(self, directory, guard, engine):
        if type(guard) is not DeviceLock:
            raise ValueError("A held device lock is required")
        guard.assert_held()
        self.directory = Path(directory)
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("An absolute private journal directory is required")
        self.guard, self.engine = guard, engine
        self.fd = self.metadata_fd = None
        self.publication_thread = threading.Lock()
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.fd = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            self.check()
            self.metadata_fd = os.open(
                "publication.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self.fd,
            )
            private(os.fstat(self.metadata_fd))
            engine.verify()
            engine_id = self.engine_id()
            lock_stat = guard.directory.stat()
            lock_namespace = [lock_stat.st_dev, lock_stat.st_ino]
            identity = self.read("installation.json")
            if identity is None:
                identity = {
                    "version": 1,
                    "installation": uuid.uuid4().hex,
                    "engine": engine_id,
                    "lock_namespace": lock_namespace,
                }
                try:
                    self.write("installation.json", identity, create=True)
                except FileExistsError:
                    # A controller for another device initialized the same root.
                    identity = self.read("installation.json")
            if (
                set(identity) != {"version", "installation", "engine", "lock_namespace"}
                or type(identity["version"]) is not int
                or identity["version"] != 1
                or type(identity["installation"]) is not str
                or not NONCE.fullmatch(identity["installation"])
                or identity["engine"] != engine_id
                or identity["lock_namespace"] != lock_namespace
            ):
                raise SandboxUnavailable("Journal belongs to a different engine or lock namespace")
            self.identity = identity
            self.filename = guard.device_uuid + ".active.json"
        except BaseException:
            self.close()
            raise

    def engine_id(self):
        try:
            return str(
                uuid.UUID(self.engine.run("info", "--format", "{{.ID}}").stdout.decode().strip())
            )
        except (ValueError, UnicodeError):
            raise SandboxUnavailable("Docker daemon identity is unavailable") from None

    def check(self):
        self.guard.assert_held()
        if self.fd is None:
            raise SandboxUnavailable("Container journal is closed")
        opened, named = os.fstat(self.fd), self.directory.lstat()
        private(opened, directory=True)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise SandboxUnavailable("Container journal directory was replaced")

    def check_engine(self):
        self.check()
        if self.read("installation.json") != self.identity:
            raise SandboxUnavailable("Persistent installation identity changed")
        if self.engine_id() != self.identity["engine"]:
            raise SandboxUnavailable("Docker daemon identity changed")

    def read(self, name):
        self.check()
        if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]{0,159}", name):
            raise ValueError("A fixed journal basename is required")
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            private(os.fstat(source.fileno()))
            data = source.read(MAX_RECORD + 1)
            if len(data) > MAX_RECORD:
                raise SandboxUnavailable("Container journal record exceeded its budget")
            return decode_frame(data)

    @contextmanager
    def publication(self):
        # All publishers share this inode, including first installation across
        # different device locks. The lock is never unlinked or replaced.
        if not self.publication_thread.acquire(timeout=2):
            raise SandboxUnavailable("Journal publication remained busy")
        try:
            end = time.monotonic() + 2
            while True:
                self.check()
                opened = os.fstat(self.metadata_fd)
                named = os.stat("publication.lock", dir_fd=self.fd, follow_symlinks=False)
                private(opened)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise SandboxUnavailable("Journal publication lock was replaced")
                try:
                    fcntl.flock(self.metadata_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise SandboxUnavailable("Journal publication remained busy") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(self.metadata_fd, fcntl.LOCK_UN)
        finally:
            self.publication_thread.release()

    def write(self, name, value, *, create=False):
        self.check()
        if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]{0,159}", name):
            raise ValueError("A fixed journal basename is required")
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_RECORD:
            raise SandboxUnavailable("Container journal record exceeded its budget")
        temporary = ".write-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            self.check()
            with self.publication():
                if create:
                    try:
                        os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError("Journal record already exists", name)
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def load(self):
        self.check_engine()
        record = self.read(self.filename)
        if record is None:
            return None
        try:
            args = record["arguments"]
            job, reservation = uuid.UUID(record["job"]), uuid.UUID(record["reservation"])
            expected_owner = owner(
                self.identity, self.guard.device_uuid, job, reservation, record["purpose"]
            )
            labels, security = record["labels"], record["security"]
            version = record["version"]
            if type(version) is not int or version not in {1, 2, 3}:
                raise ValueError
            profile = SUPERVISION_PROFILE if version == 1 else record["resource_profile"]
            if version >= 2 and profile != WORKLOAD_PROFILE:
                raise ValueError
            keys = KEYS | ({"resource_profile"} if version >= 2 else set())
            if version == 3:
                keys.add("device_spec")
                selection = validate_selection(record["device_spec"])
                if selection["uuid"] != record["device"]:
                    raise ValueError
            if (
                set(record) != keys
                or any(
                    record[key] != self.identity[key]
                    for key in ("installation", "engine", "lock_namespace")
                )
                or record["device"] != self.guard.device_uuid
                or record["owner"] != expected_owner
                or record["name"] != "praximesh-gpu-container-" + expected_owner[:40]
                or record["purpose"] not in {"task", "maintenance", "recovery"}
                or not IMAGE_ID.fullmatch(record["image"])
                or set(labels) != {PREFIX + "protocol", PREFIX + "runtime"}
                or labels[PREFIX + "protocol"] != PROTOCOL
                or not HEX_ID.fullmatch(labels[PREFIX + "runtime"])
                or type(security) is not list
                or len(security) != 2
                or security[0] != "no-new-privileges"
                or type(security[1]) is not dict
                or type(record["image_env"]) is not list
                or any(type(item) is not str for item in record["image_env"])
                or type(args) is not list
                or len(args) < 5
                or any(type(item) is not str or not item or "\0" in item for item in args)
                or args[3] != resource_marker(profile)
                or not NONCE.fullmatch(args[2])
                or not Path(args[4]).is_absolute()
                or not all(math.isfinite(float(x)) for x in args[:2])
                or not 0 < float(args[1]) - float(args[0]) <= MAX_SECONDS
                or record["phase"] not in {"intent", "created", "running", "removed"}
                or (
                    record["container_id"] is not None
                    and not HEX_ID.fullmatch(record["container_id"])
                )
                or (
                    record["started_at"] is not None
                    and (
                        type(record["started_at"]) is not str
                        or not 1 <= len(record["started_at"]) <= 128
                    )
                )
                or (
                    record["phase"] == "intent"
                    and (record["container_id"] is not None or record["started_at"] is not None)
                )
                or (record["phase"] in {"created", "running"} and record["container_id"] is None)
                or (record["phase"] == "created" and record["started_at"] is not None)
                or (record["phase"] == "running" and record["started_at"] is None)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SandboxUnavailable("Invalid or unsupported durable container record") from None
        return record

    def begin(self, service, arguments):
        if self.load() is not None:
            raise SandboxUnavailable(
                "Recover the previous device container before creating another"
            )
        record = {
            **self.identity,
            "device": self.guard.device_uuid,
            "job": str(service.job_id),
            "reservation": str(service.reservation_token),
            "purpose": service.purpose,
            "owner": service.owner,
            "name": service.name,
            "image": service.image,
            "labels": service.required_image_labels(),
            "security": ["no-new-privileges", seccomp_profile()],
            "image_env": service.image_env,
            "arguments": arguments,
            "container_id": None,
            "started_at": None,
            "phase": "intent",
        }
        if service.resource_profile != SUPERVISION_PROFILE:
            record.update(version=2, resource_profile=service.resource_profile)
        selection = service.journal_device_spec()
        if selection is not None:
            validate_selection(selection)
            if (
                selection["uuid"] != record["device"]
                or service.resource_profile != WORKLOAD_PROFILE
            ):
                raise SandboxUnavailable("Device task does not match its lock or total budget")
            record.update(version=3, device_spec=selection)
        self.write(self.filename, record, create=True)

    def update(self, service, phase):
        record = self.load()
        if record is None or record["owner"] != service.owner:
            raise SandboxUnavailable("Original container intent is missing")
        expected = "intent" if phase == "created" else "created"
        if phase not in {"created", "running"} or record["phase"] != expected:
            raise SandboxUnavailable("Unexpected durable container transition")
        record.update(
            phase=phase,
            container_id=service.container_id,
            started_at=service.started_at if phase == "running" else None,
        )
        self.write(self.filename, record)

    def _stop_original(self):
        record = self.load()
        if record is None:
            return None
        service = RecoveryContainer(self, record)
        state = service.inspect()
        if state is not None and record["container_id"] is None:
            # Fence a create whose CLI never returned before any destructive action.
            record = {
                **record,
                "container_id": service.container_id,
                "started_at": service.started_at,
                "phase": "running" if state["running"] else "created",
            }
            self.write(self.filename, record)
        service.stop()
        return record, service

    def stop(self):
        """Stop the original instance but retain its active record for DB fencing."""
        stopped = self._stop_original()
        if stopped is None:
            return None
        _record, service = stopped
        return {"container": service.container_id, "stopped": True, "ready": False}

    def recover(self):
        stopped = self._stop_original()
        if stopped is None:
            return None
        record, service = stopped
        service.remove()
        # No new execution or claimed device recovery: only this container is gone.
        removed = {**record, "phase": "removed"}
        self.write(self.filename, removed)
        history = self.guard.device_uuid + "." + record["arguments"][2] + ".done.json"
        old = self.read(history)
        if old is not None and old != removed:
            raise SandboxUnavailable("Container recovery archive identity changed")
        if old is None:
            self.write(history, removed, create=True)
        self.check()
        if self.read(self.filename) != removed:
            raise SandboxUnavailable("Container record changed during recovery")
        os.unlink(self.filename, dir_fd=self.fd)
        os.fsync(self.fd)
        return {
            "container": service.container_id,
            "removed": True,
            "ready": False,
            "history": history,
        }

    def close(self):
        if self.metadata_fd is not None:
            os.close(self.metadata_fd)
            self.metadata_fd = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class PersistentRuntimeService(RuntimeContainerService):
    SCOPE = SCOPE

    def __init__(
        self,
        journal,
        job_id,
        reservation_token,
        *,
        image,
        purpose="task",
        resource_profile=SUPERVISION_PROFILE,
    ):
        self.journal, self.purpose = journal, purpose
        journal.check_engine()
        super().__init__(
            journal.guard,
            job_id,
            reservation_token,
            engine=journal.engine,
            image=image,
            purpose=purpose,
            resource_profile=resource_profile,
        )
        self.owner = owner(
            journal.identity, self.guard.device_uuid, self.job_id, self.reservation_token, purpose
        )
        self.name = "praximesh-gpu-container-" + self.owner[:40]

    def record_intent(self, arguments):
        self.journal.begin(self, arguments)

    def journal_device_spec(self):
        return None

    def record_created(self):
        self.journal.update(self, "created")

    def record_started(self, status):
        self.journal.update(self, "running")


class RecoveryContainer(ContainerService):
    """Stop/remove with the saved version's budget; never starts or attaches."""

    def __init__(self, journal, record):
        self.journal, self.record = journal, record
        super().__init__(
            journal.guard,
            record["job"],
            record["reservation"],
            engine=journal.engine,
            image=record["image"],
            purpose=record["purpose"],
            resource_profile=record.get("resource_profile", SUPERVISION_PROFILE),
        )
        self.owner, self.name = record["owner"], record["name"]
        self.container_id, self.started_at = record["container_id"], record["started_at"]
        self.image_env = record["image_env"]

    def _verify_image(self):
        self.journal.check_engine()

    def required_image_labels(self):
        return self.record["labels"]

    def valid_devices(self, value):
        if self.record["version"] == 3:
            return value == docker_devices(self.record["device_spec"])
        return super().valid_devices(value)

    def valid_security_options(self, options):
        if type(options) is not list or len(options) != 2 or options[0] != "no-new-privileges":
            return False
        try:
            return (
                type(options[1]) is str
                and options[1].startswith("seccomp=")
                and decode_frame(options[1][8:]) == self.record["security"][1]
            )
        except SandboxUnavailable:
            return False

    def inspect(self):
        state = super().inspect()
        if state is not None and state["arguments"] != self.record["arguments"]:
            raise SandboxUnavailable("Container does not match the durable startup intent")
        return state

    def start(self, *_args, **_kwargs):
        raise SandboxUnavailable("Recovery cannot execute an old container image")
