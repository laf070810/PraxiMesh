"""Private release-generation binding for coordinator-owned GPU worker settings.

Reads only original files and inode identities. This is not a readiness check,
configuration publisher, database reservation check, or GPU authorization.
"""

import hashlib
import json
import os
import re

from judge.container_journal import private
from judge.container_service import IMAGE_ID
from judge.gpu_session import decode_frame

OWNER = "deployment-owner.json"
GENERATION = "deployment-generation.json"
TRANSITION = "deployment-config-transition.json"
MAX_METADATA = 32 * 1024
SHA256 = re.compile(r"[a-f0-9]{64}")
NONCE = re.compile(r"[a-f0-9]{32}")
DATABASE = re.compile(r"px_([a-z][a-z0-9]{0,19})_([a-f0-9]{12})")
FIELDS = {
    "schema",
    "site_id",
    "binding_sha256",
    "generation",
    "database",
    "release_sha256",
    "worker_image",
    "worker_contract",
    "configuration_sha256",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_record(config, owner, record):
    from judge.container_worker_config import database_settings

    try:
        database = database_settings(config)
        match = DATABASE.fullmatch(record["database"])
        valid = (
            type(record) is dict
            and set(record) == FIELDS
            and type(record["schema"]) is int
            and record["schema"] == 1
            and type(owner) is dict
            and set(owner) == {"schema", "id", "site_id", "coordinator", "node"}
            and type(owner["schema"]) is int
            and owner["schema"] == 1
            and NONCE.fullmatch(owner["id"])
            and NONCE.fullmatch(owner["site_id"])
            and set(owner["node"]) == {"config", "mounts_sha256", "installation", "lock"}
            and record["site_id"] == owner["site_id"]
            and record["binding_sha256"] == digest(owner)
            and record["configuration_sha256"] == digest(config)
            and owner["node"]["config"]["device"] == config["gpu"]
            and match is not None
            and match[2] == record["generation"]
            and database["NAME"] == record["database"]
            and database["USER"] == f"px_{match[1]}_worker"
            and config["worker_id"] == f"px-{match[1]}-{match[2]}-gpu-worker"
            and IMAGE_ID.fullmatch(record["worker_image"])
            and all(
                SHA256.fullmatch(record[key])
                for key in (
                    "binding_sha256",
                    "configuration_sha256",
                    "release_sha256",
                    "worker_contract",
                )
            )
        )
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(
            "Private GPU generation does not match its original configuration and binding"
        )
    return record


def generation_record(owner, generation, config):
    """Describe a verified release's configuration; does not write or activate it."""
    record = {
        "schema": 1,
        "site_id": owner["site_id"],
        "binding_sha256": digest(owner),
        "generation": generation["id"],
        "database": generation["database"],
        "release_sha256": generation["release_sha256"],
        "worker_image": generation["manifest"]["images"]["gpu_worker"],
        "worker_contract": generation["manifest"]["gpu"]["contracts"]["worker"],
        "configuration_sha256": digest(config),
    }
    return validate_record(config, owner, record)


def read(directory_fd, name, *, optional=False):
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
        raw = source.read(MAX_METADATA + 1)
        if len(raw) > MAX_METADATA:
            raise ValueError("Private GPU generation metadata exceeded its bound")
        return raw


def inode(info):
    return [info.st_dev, info.st_ino]


def check_generation(directory_fd, config, *, worker_contract=None):
    if read(directory_fd, TRANSITION, optional=True) is not None:
        raise ValueError("GPU generation configuration publication is unfinished")
    owner_raw = read(directory_fd, OWNER, optional=True)
    record_raw = read(directory_fd, GENERATION, optional=True)
    if owner_raw is None and record_raw is None:
        return  # Original standalone workers do not belong to a coordinator.
    if owner_raw is None or record_raw is None:
        raise ValueError("Coordinator-owned worker configuration requires its generation record")
    owner, record = decode_frame(owner_raw), decode_frame(record_raw)
    validate_record(config, owner, record)
    if worker_contract is not None and record["worker_contract"] != worker_contract:
        raise ValueError("Worker image contract differs from its deployment generation")
    mounts_raw = read(directory_fd, "mounts.json")
    mounts = decode_frame(mounts_raw)
    node = owner["node"]
    if (
        hashlib.sha256(mounts_raw).hexdigest() != node["mounts_sha256"]
        or mounts.get("version") != 4
        or mounts.get("root") != inode(os.fstat(directory_fd))
        or mounts["device_observer"]["target"]["uuid"] != config["gpu"]
    ):
        raise ValueError("Original GPU mount identity changed")
    for name in ("locks", "journal"):
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        try:
            details = os.fstat(fd)
            private(details, directory=True)
            if inode(details) != mounts[name]:
                raise ValueError("Original GPU state directory changed")
            if name == "locks":
                lock = os.open(
                    config["gpu"] + ".lock",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=fd,
                )
                try:
                    details = os.fstat(lock)
                    private(details)
                    if inode(details) != node["lock"]:
                        raise ValueError("Original GPU device lock changed")
                finally:
                    os.close(lock)
            else:
                installation = decode_frame(read(fd, "installation.json"))
                if canonical(installation) != canonical(node["installation"]):
                    raise ValueError("Original GPU installation changed")
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            private(named, directory=True)
            if inode(named) != inode(os.fstat(fd)):
                raise ValueError("Original GPU directory changed during configuration read")
        finally:
            os.close(fd)
    if (
        read(directory_fd, OWNER) != owner_raw
        or read(directory_fd, GENERATION) != record_raw
        or read(directory_fd, TRANSITION, optional=True) is not None
    ):
        raise ValueError("GPU generation metadata changed during configuration read")
    return record
