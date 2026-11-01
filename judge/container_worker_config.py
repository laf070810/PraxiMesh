"""Private application configuration, never sent to a task or Docker arguments."""

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from judge.container_controller import STATE
from judge.container_deployment_config import check_generation
from judge.container_journal import private
from judge.container_service import IMAGE_ID
from judge.driver_artifacts import validate_manifest
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_session import decode_frame

MAX_CONFIG = 32 * 1024


def validate_config(value):
    if (
        type(value) is not dict
        or set(value)
        != {"schema", "enabled", "gpu", "worker_id", "task_image", "driver_manifest", "database"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or type(value["enabled"]) is not bool
        or type(value["gpu"]) is not str
        or not GPU_UUID.fullmatch(value["gpu"])
        or type(value["worker_id"]) is not str
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value["worker_id"])
        or type(value["task_image"]) is not str
        or not IMAGE_ID.fullmatch(value["task_image"])
    ):
        raise ValueError("Invalid container worker configuration")
    validate_manifest(value["driver_manifest"])
    database_settings(value)
    return value


def database_settings(value):
    database = value["database"]
    if (
        type(database) is not dict
        or set(database) != {"url", "sslmode"}
        or type(database["url"]) is not str
        or not 1 <= len(database["url"]) <= 4096
        or any(ord(char) < 33 for char in database["url"])
        or database["sslmode"] not in {"disable", "require", "verify-full"}
    ):
        raise ValueError("Invalid private PostgreSQL configuration")
    parsed = urlsplit(database["url"])
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.username
        or not parsed.password
        or not parsed.path.startswith("/")
        or not parsed.path[1:]
        or "/" in unquote(parsed.path[1:])
        or parsed.query
        or parsed.fragment
        or parsed.port is not None
        and not 1 <= parsed.port <= 65535
    ):
        raise ValueError("An explicit PostgreSQL host, database and worker login are required")
    options = {"sslmode": database["sslmode"], "connect_timeout": 5}
    if database["sslmode"] == "verify-full":
        options["sslrootcert"] = str(STATE / "database-ca.pem")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path[1:]),
        "USER": unquote(parsed.username),
        "PASSWORD": unquote(parsed.password),
        "HOST": parsed.hostname,
        "PORT": parsed.port or 5432,
        "OPTIONS": options,
        "CONN_MAX_AGE": 0,
    }


def read_config(directory=STATE, *, worker_contract=None, with_generation=False):
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("Worker state must use an absolute path without links")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        private(os.fstat(fd), directory=True)
        source_fd = os.open(
            "worker.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
        )
        with os.fdopen(source_fd, "rb") as source:
            private(os.fstat(source.fileno()))
            data = source.read(MAX_CONFIG + 1)
            if len(data) > MAX_CONFIG:
                raise ValueError("Worker configuration exceeds its size limit")
            value = validate_config(decode_frame(data))
        generation = check_generation(fd, value, worker_contract=worker_contract)
        if value["database"]["sslmode"] == "verify-full":
            ca_fd = os.open(
                "database-ca.pem", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                private(os.fstat(ca_fd))
            finally:
                os.close(ca_fd)
        opened, named = os.fstat(fd), directory.lstat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("Worker state changed while reading configuration")
        return (value, generation) if with_generation else value
    finally:
        os.close(fd)
