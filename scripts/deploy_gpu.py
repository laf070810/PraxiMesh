"""Bind original private GPU state to one release coordinator, without device access.

This module reads filesystem/daemon identities only. Binding does not authorize
execution, adopt an old worker/database, recover a task, or publish readiness.
"""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from judge.container_controller import identity, manifest
from judge.container_deployment_config import OWNER, generation_record
from judge.container_journal import private
from judge.container_service import LocalEngine
from judge.container_worker_config import validate_config
from judge.gpu_device_lock import GPU_UUID, DeviceLock
from judge.gpu_session import decode_frame

MAX_BYTES = 32 * 1024
NONCE = re.compile(r"[a-f0-9]{32}")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def configuration(value):
    if (
        type(value) is not dict
        or set(value) != {"state", "docker_socket", "device"}
        or type(value["device"]) is not str
        or not GPU_UUID.fullmatch(value["device"])
        or value["device"] != "GPU-" + value["device"][4:].lower()
    ):
        raise ValueError("Expected one canonical GPU UUID and original local state/socket")
    for name in ("state", "docker_socket"):
        raw = value[name]
        if type(raw) is not str or not re.fullmatch(r"/[A-Za-z0-9_./-]+", raw):
            raise ValueError("GPU paths must be explicit normalized absolute paths")
        path = Path(raw)
        if str(path) != raw or path.resolve() != path:
            raise ValueError("GPU paths must not contain links or traversal")
    return value


def read_private(directory_fd, name, *, optional=False):
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        if optional:
            return None
        raise
    with os.fdopen(fd, "rb") as source:
        private(os.fstat(source.fileno()))
        raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("GPU deployment metadata exceeded its bound")
        return raw


class GPUDeploymentNode:
    def __init__(self, config, coordinator, site_id, ambient_engine):
        self.config = configuration(json.loads(encoded(config)))
        self.root = Path(config["state"])
        self.coordinator = Path(coordinator)
        if (
            not self.coordinator.is_absolute()
            or self.coordinator.resolve() != self.coordinator
            or self.coordinator == self.root
            or self.root in self.coordinator.parents
            or type(site_id) is not str
            or not NONCE.fullmatch(site_id)
        ):
            raise ValueError("Distinct original coordinator and GPU state are required")
        private(self.coordinator.lstat(), directory=True)
        self.coordinator_identity = identity(self.coordinator)
        self.site_id, self.ambient_engine = site_id, ambient_engine
        self.engine = LocalEngine(config["docker_socket"])
        self.fd = self.journal_fd = self.locks_fd = self.guard = None
        self.snapshot = None

    def check(self):
        if self.fd is None or self.guard is None:
            raise ValueError("GPU state changes require the original held lock")
        self.guard.assert_held()
        for fd, path in (
            (self.fd, self.root),
            (self.journal_fd, self.root / "journal"),
            (self.locks_fd, self.root / "locks"),
        ):
            info = os.fstat(fd)
            private(info, directory=True)
            if [info.st_dev, info.st_ino] != identity(path):
                raise ValueError("GPU state directory identity changed")
        private(self.coordinator.lstat(), directory=True)
        if identity(self.coordinator) != self.coordinator_identity:
            raise ValueError("Original coordinator directory changed")
        self.engine.check_socket()

    @contextmanager
    def locked(self):
        if self.fd is not None:
            raise ValueError("GPU node is already open")
        descriptors = []
        try:
            for name, path in (
                ("fd", self.root),
                ("journal_fd", self.root / "journal"),
                ("locks_fd", self.root / "locks"),
            ):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append(fd)
                setattr(self, name, fd)
                private(os.fstat(fd), directory=True)
            # Never let DeviceLock create a second/missing installation namespace.
            lock_name = self.config["device"] + ".lock"
            read_private(self.locks_fd, lock_name)
            original_lock = identity(self.root / "locks" / lock_name)
            with DeviceLock(self.root / "locks", self.config["device"]) as guard:
                self.guard = guard
                self.check()
                if identity(self.root / "locks" / lock_name) != original_lock:
                    raise ValueError("Original device lock changed")
                raw = read_private(self.fd, "mounts.json")
                mounts = decode_frame(raw)
                if type(mounts) is not dict or mounts.get("version") != 4:
                    raise ValueError("A prepared full v4 mount contract is required")
                expected = manifest(
                    self.root,
                    self.engine,
                    host_processes=True,
                    device_observer=mounts["device_observer"],
                )
                if (
                    encoded(mounts) != encoded(expected)
                    or mounts["device_observer"]["target"]["uuid"] != self.config["device"]
                    or mounts["engine"] != self.ambient_engine
                ):
                    raise ValueError("Original mounts, device or deployment daemon do not match")
                installation = decode_frame(read_private(self.journal_fd, "installation.json"))
                if (
                    type(installation) is not dict
                    or set(installation) != {"version", "installation", "engine", "lock_namespace"}
                    or type(installation["version"]) is not int
                    or installation["version"] != 1
                    or type(installation["installation"]) is not str
                    or not NONCE.fullmatch(installation["installation"])
                    or installation["engine"] != mounts["engine"]
                    or encoded(installation["lock_namespace"]) != encoded(mounts["locks"])
                ):
                    raise ValueError("Original journal installation does not match")
                read_private(self.journal_fd, "publication.lock")
                self.snapshot = {
                    "config": self.config,
                    "mounts_sha256": hashlib.sha256(raw).hexdigest(),
                    "installation": installation,
                    "lock": original_lock,
                }
                yield self
                self.check()
        finally:
            self.guard = self.snapshot = None
            for fd in reversed(descriptors):
                os.close(fd)
            self.fd = self.journal_fd = self.locks_fd = None

    def proposal(self, nonce):
        self.check()
        if type(nonce) is not str or not NONCE.fullmatch(nonce):
            raise ValueError("A durable GPU attachment intent is required")
        return {
            "schema": 1,
            "id": nonce,
            "site_id": self.site_id,
            "coordinator": {
                "path": str(self.coordinator),
                "identity": self.coordinator_identity,
            },
            "node": self.snapshot,
        }

    def validate_binding(self, binding):
        if type(binding) is not dict or encoded(binding) != encoded(
            self.proposal(binding.get("id"))
        ):
            raise ValueError("GPU binding belongs to different original state")

    def owner(self):
        self.check()
        raw = read_private(self.fd, OWNER, optional=True)
        return None if raw is None else decode_frame(raw)

    def require_owner(self, binding):
        self.validate_binding(binding)
        if hashlib.sha256(read_private(self.fd, "mounts.json")).hexdigest() != self.snapshot[
            "mounts_sha256"
        ] or encoded(decode_frame(read_private(self.journal_fd, "installation.json"))) != encoded(
            self.snapshot["installation"]
        ):
            raise ValueError("Original GPU mount or installation changed while locked")
        if encoded(self.owner()) != encoded(binding):
            raise ValueError("Original GPU ownership marker is missing or changed")

    def generation_configuration(self, binding, generation, database, *, enabled=False):
        """Render private values under the original lock; never publish or start."""
        self.require_owner(binding)
        release = generation["manifest"]
        if type(release.get("format")) is not int or release["format"] != 2:
            raise ValueError("A verified four-image release is required")
        protocol_source = "judge/container_deployment_config.py"
        if protocol_source not in release.get("source_files", {}) or any(
            protocol_source not in release["gpu"].get("source_scopes", {}).get(scope, [])
            for scope in ("execution", "controller", "worker")
        ):
            raise ValueError("This frozen release does not support deployment generation binding")
        mounts = decode_frame(read_private(self.fd, "mounts.json"))
        observer = mounts["device_observer"]
        if (
            encoded(release["gpu"]["management"]) != encoded(observer["library"])
            or release["gpu"]["driver"]["driver_version"] != observer["target"]["driver"]
        ):
            raise ValueError(
                "Release driver and management artifacts differ from the original node"
            )
        # JSON round-trip prevents the caller mutating a referenced release/config
        # object after the exact private configuration digest has been derived.
        value = validate_config(
            json.loads(
                encoded(
                    {
                        "schema": 1,
                        "enabled": enabled,
                        "gpu": self.config["device"],
                        "worker_id": generation["gpu_worker"],
                        "task_image": release["images"]["gpu_task"],
                        "driver_manifest": release["gpu"]["driver"],
                        "database": database,
                    }
                )
            )
        )
        record = generation_record(binding, generation, value)
        self.require_owner(binding)
        return value, record

    @staticmethod
    def temporary(binding):
        return ".deployment-owner-" + binding["id"] + ".tmp"

    def require_fresh(self, intent=None):
        self.check()
        allowed = {"locks", "journal", "mounts.json"}
        if intent is not None:
            self.validate_binding(intent)
            allowed |= {OWNER, self.temporary(intent)}
        if (
            set(os.listdir(self.fd)) - allowed
            or set(os.listdir(self.journal_fd)) != {"installation.json", "publication.lock"}
            or set(os.listdir(self.locks_fd)) != {self.config["device"] + ".lock"}
        ):
            raise ValueError(
                "First attachment requires unused prepared state, without worker or task records"
            )

    def publish_owner(self, binding):
        self.validate_binding(binding)
        existing = self.owner()
        if existing is not None and encoded(existing) != encoded(binding):
            raise ValueError("GPU state is already assigned to another attachment")
        temporary = self.temporary(binding)
        # A killed writer may leave its exact intent-specific, private temporary.
        # Never sweep unknown files or unlink the persistent device lock.
        if read_private(self.fd, temporary, optional=True) is not None:
            os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        if existing is not None:
            os.fsync(self.fd)
            return
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(encoded(binding) + b"\n")
                target.flush()
                os.fsync(target.fileno())
            self.check()
            if self.owner() is not None:
                raise ValueError("GPU owner appeared outside the original device lock")
            # Both coordinator and device locks serialize the only supported writers.
            os.rename(temporary, OWNER, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass
