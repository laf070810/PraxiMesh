"""Prepare private worker settings; optional initialization reads selected GPU metadata.

Never starts a workload, changes a daemon or publishes readiness.
"""

import argparse
import json
import os
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import manifest  # noqa: E402
from judge.container_journal import ContainerJournal, private  # noqa: E402
from judge.container_monitor import validate_observer  # noqa: E402
from judge.container_service import IMAGE_ID, LocalEngine  # noqa: E402
from judge.container_worker_config import MAX_CONFIG, validate_config  # noqa: E402
from judge.driver_artifacts import validate_manifest  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_monitor import identify, processes, query  # noqa: E402
from judge.gpu_session import decode_frame  # noqa: E402
from judge.nvml_artifacts import validate_manifest as validate_management  # noqa: E402


def read_private(path, *, artifact=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if artifact:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Expected a regular local manifest")
        else:
            private(info)
        data = source.read(MAX_CONFIG + 1)
        if len(data) > MAX_CONFIG:
            raise ValueError("Private input exceeds its size limit")
        return data


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


def configure(directory, worker_id, task_image, driver_manifest, database, *, enabled=False):
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("An existing absolute private state directory is required")
    private(directory.lstat(), directory=True)
    mounts = decode_frame(read_private(directory / "mounts.json"))
    if mounts.get("version") != 4:
        raise ValueError("Prepare the original full v4 observation mount contract first")
    observer = validate_observer(mounts["device_observer"])
    if observer["schema"] != 2 or driver_manifest["driver_version"] != observer["target"]["driver"]:
        raise ValueError("Task driver and full management observation must match")
    value = validate_config(
        {
            "schema": 1,
            "enabled": enabled,
            "gpu": observer["target"]["uuid"],
            "worker_id": worker_id,
            "task_image": task_image,
            "driver_manifest": driver_manifest,
            "database": database,
        }
    )
    # Never replace a live worker's configuration or create a second lock namespace.
    with DeviceLock(directory / "locks", value["gpu"]):
        create_json(directory / "worker.json", value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--task-image", required=True)
    parser.add_argument("--driver-manifest", required=True, type=Path)
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="New installation only: explicitly read selected idle GPU metadata and create persistent mount identities.",
    )
    parser.add_argument("--gpu", help="Full UUID authorized for --initialize; never an index.")
    parser.add_argument("--docker-socket", help="Private rootless socket for --initialize.")
    parser.add_argument(
        "--worker-image-manifest",
        type=Path,
        help="Pinned worker-image.json from the worker build, required with --initialize.",
    )
    parser.add_argument(
        "--database-file",
        required=True,
        type=Path,
        help="Private 0600 JSON with url and sslmode; never pass credentials on CLI.",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Explicitly allow full GPU startup when the configured worker is launched.",
    )
    args = parser.parse_args()
    try:
        driver = validate_manifest(decode_frame(read_private(args.driver_manifest, artifact=True)))
        database = decode_frame(read_private(args.database_file))
        if args.initialize:
            if not all((args.gpu, args.docker_socket, args.worker_image_manifest)):
                raise ValueError(
                    "New installation needs a selected UUID, socket and worker image manifest"
                )
            image = decode_frame(read_private(args.worker_image_manifest, artifact=True))
            if image.get("schema") != 1 or not IMAGE_ID.fullmatch(image.get("image", "")):
                raise ValueError("Invalid pinned worker image manifest")
            initialize_state(
                args.state, LocalEngine(args.docker_socket), args.gpu, image["management_manifest"]
            )
        elif any((args.gpu, args.docker_socket, args.worker_image_manifest)):
            raise ValueError("Metadata initialization options require --initialize")
        configure(
            args.state, args.worker_id, args.task_image, driver, database, enabled=args.enable
        )
        print("Private worker configuration created; no workload or readiness was started.")
    except Exception as error:
        print("Container worker configuration failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
