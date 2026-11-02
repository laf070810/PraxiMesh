"""Explicit first physical-state preparation, shared by worker and local tools.

Only initialize_state reads selected GPU metadata. Imports and validators do not.
It never takes over existing state, stops foreign processes or publishes ready.
"""

import json
import os
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from judge.container_controller import manifest
from judge.container_journal import ContainerJournal
from judge.container_monitor import validate_observer
from judge.gpu_device_lock import DeviceLock
from judge.gpu_monitor import identify, processes, query
from judge.nvml_artifacts import validate_manifest as validate_management


def create_json(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def initialize_state(directory, engine, device, management):
    """Explicit new installation only; caller must authorize metadata access."""
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory or directory.exists():
        raise ValueError("Initialization requires a new absolute private state directory")
    validate_management(management)
    if management["schema"] != 2:
        raise ValueError("The full pinned management bundle is required")
    target = identify(device)
    observation = query("-i", device, "--query", "--xml-format")
    if processes(observation, target):
        raise ValueError("Selected GPU has external processes; nothing was stopped")
    minor = ET.fromstring(observation).findtext("gpu/minor_number", "")
    if not minor.isascii() or not minor.isdecimal():
        raise ValueError("The selected physical device minor is missing")
    observer = validate_observer(
        {
            "schema": 2,
            "target": target,
            "minor": int(minor),
            "library": management,
        }
    )
    engine.verify()
    directory.mkdir(mode=0o700)
    for name in ("locks", "journal"):
        (directory / name).mkdir(mode=0o700)
    with DeviceLock(directory / "locks", device) as guard:
        with ContainerJournal(directory / "journal", guard, engine):
            create_json(
                directory / "mounts.json",
                manifest(
                    directory,
                    engine,
                    host_processes=True,
                    device_observer=observer,
                ),
            )
    return observer
