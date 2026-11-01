"""Pinned trusted-controller image builder; does not start any container."""

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

from judge.container_controller import (
    CLI_ARCHIVE_SHA256,
    CLI_SHA256,
    CONTROLLER_SOURCES,
    controller_digest,
)

ROOT = Path(__file__).resolve().parents[1]


def stage_cli(archive, destination):
    snapshot = destination.parent / "docker.tgz"
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source, snapshot.open("xb") as target:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024**2:
            raise ValueError("A bounded regular CLI archive is required")
        digest, count = hashlib.sha256(), 0
        while data := source.read(1024**2):
            count += len(data)
            if count > info.st_size:
                raise ValueError("CLI archive changed during staging")
            digest.update(data)
            target.write(data)
        if count != info.st_size or digest.hexdigest() != CLI_ARCHIVE_SHA256:
            raise ValueError("CLI archive digest mismatch")
    with tarfile.open(snapshot) as archive_file:
        members = [member for member in archive_file if member.name == "docker/docker"]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 64 * 1024**2:
            raise ValueError("Unexpected CLI archive member")
        data = archive_file.extractfile(members[0]).read(64 * 1024**2 + 1)
        if hashlib.sha256(data).hexdigest() != CLI_SHA256:
            raise ValueError("CLI binary digest mismatch")
    destination.write_bytes(data)
    destination.chmod(0o755)
    snapshot.unlink()


def build_controller(engine, archive, out, tag, *, nvml_library_dir=None, full_inventory=False):
    if full_inventory and nvml_library_dir is None:
        raise ValueError("Full process inventory requires the management library")
    context = out / "context"
    context.mkdir(mode=0o700)
    stage_cli(archive, context / "docker")
    expected = controller_digest()
    for name in CONTROLLER_SOURCES:
        target = context / "source" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copyfile(ROOT / "deploy/container-controller.Dockerfile", context / "Dockerfile")
    extra = []
    if nvml_library_dir is not None:
        from judge.nvml_artifacts import manifest_digest
        from scripts.build_container_monitor import snapshot_management

        observer = snapshot_management(
            nvml_library_dir, context / "observer", full_inventory=full_inventory
        )
        extra = ["--build-arg", "OBSERVER_CONTRACT=" + manifest_digest(observer)]
        with (context / "Dockerfile").open("ab") as target:
            target.write((ROOT / "deploy/container-monitor.fragment").read_bytes())
    config = out / "docker-config"
    config.mkdir(mode=0o700)
    with (out / "build.log").open("w") as log:
        subprocess.run(
            [
                "docker",
                "--config",
                str(config),
                "--host",
                "unix://" + str(engine.path),
                "build",
                "--tag",
                tag,
                "--iidfile",
                str(out / "image-id"),
                "--build-arg",
                "CONTROLLER_CONTRACT=" + expected,
                *extra,
                str(context),
            ],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )
    if expected != controller_digest():
        raise RuntimeError("Controller source changed during build")
    if nvml_library_dir is not None:
        from scripts.build_container_monitor import verify_installed

        verify_installed(nvml_library_dir, observer)
    return (out / "image-id").read_text().strip()
