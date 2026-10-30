"""Owned, credential-free namespaces for the trusted GPU controller.

This is process infrastructure, not a GPU sandbox or an admission receipt. Only
trusted platform entry points may be launched; learner code uses GPUSession.
"""

import json
import os
import re
import shutil
import stat
from pathlib import Path

from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable

WORKLOAD_LIMITS = {
    "cpu.max": "50000 100000",
    "memory.max": "536870912",
    "memory.swap.max": "0",
    "pids.max": "256",
}


def private_directory(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SandboxUnavailable("Controller directory must be owned and private")
    return info


def mounted_paths():
    """Decode kernel mountinfo paths, including escaped spaces and backslashes."""
    paths = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields[6:]:
            raise SandboxUnavailable("Cannot verify workspace mount boundaries")
        paths.append(Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])))
    return paths


class JobWorkspace:
    """Persistent task identity; reopening never implicitly deletes or reuses it."""

    def __init__(self, directory, service, *, create=True):
        if type(service) is not OwnedJobService:
            raise ValueError("An owned task service is required")
        service.guard.assert_held()
        directory = Path(directory)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ValueError("An absolute private workspace root is required")
        self.service, self.directory = service, directory
        self.name = service.unit.removeprefix("praximesh-gpu-").removesuffix(".service")
        self.path = directory / self.name
        self._root_fd = self._fd = None
        self.identity = {
            "version": 1,
            "uid": os.getuid(),
            "device": service.guard.device_uuid,
            "job": str(service.job_id),
            "reservation": str(service.reservation_token),
            "unit": service.unit,
        }
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            self._root_fd = os.open(directory, flags)
            private_directory(self._root_fd)
            if create:
                os.mkdir(self.name, mode=0o700, dir_fd=self._root_fd)
            self._fd = os.open(self.name, flags, dir_fd=self._root_fd)
            private_directory(self._fd)
            if create:
                identity_fd = os.open(
                    "identity.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self._fd,
                )
                with os.fdopen(identity_fd, "w") as stream:
                    json.dump(self.identity, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(self._fd)
                os.fsync(self._root_fd)
                for name in ("config", "cache", "runtime-env", "temporary", "data-cache", "home"):
                    os.mkdir(name, mode=0o700, dir_fd=self._fd)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        self.service.guard.assert_held()
        if self._fd is None or self._root_fd is None:
            raise SandboxUnavailable("Controller workspace is closed")
        for fd, info in (
            (self._root_fd, self.directory.lstat()),
            (self._fd, os.stat(self.name, dir_fd=self._root_fd, follow_symlinks=False)),
        ):
            opened = private_directory(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise SandboxUnavailable("Controller workspace directory was replaced")
        fd = os.open("identity.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 4096
            ):
                raise SandboxUnavailable("Controller workspace identity is not private")
            identity = stream.read(4097)
        if identity != json.dumps(self.identity, sort_keys=True).encode():
            raise SandboxUnavailable("Controller workspace belongs to a different reservation")

    def command(self, command):
        """Return a fixed trusted command with no inherited application secrets."""
        self.check()
        if len(str(self.path / "state/api.sock").encode()) >= 108:
            raise ValueError("Use a shorter workspace root for the RootlessKit socket")
        if (
            type(command) not in {list, tuple}
            or not command
            or any(type(part) is not str or not part or "\0" in part for part in command)
            or not Path(command[0]).is_absolute()
        ):
            raise ValueError("An absolute trusted controller command is required")
        environment = {
            # Podman/image libraries consult the child's home in addition to
            # XDG paths. Give the isolated process its own empty home directory.
            "HOME": str(self.path / "home"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GOMAXPROCS": "2",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": str(self.path / "config"),
            "XDG_CACHE_HOME": str(self.path / "cache"),
            "XDG_DATA_HOME": str(self.path / "data-cache"),
            "XDG_RUNTIME_DIR": str(self.path / "runtime-env"),
            "TMPDIR": str(self.path / "temporary"),
        }
        return [
            "/usr/bin/env",
            "-i",
            *(name + "=" + value for name, value in environment.items()),
            "/usr/bin/rootlesskit",
            "--state-dir=" + str(self.path / "state"),
            "--pidns",
            "--cgroupns",
            "--ipcns",
            "--utsns",
            "--evacuate-cgroup2=manager",
            "--net=none",
            "--copy-up=/etc",
            "--copy-up=/run",
            "--copy-up=/run/lock",
            *command,
        ]

    def remove_after_stop(self):
        """Delete only this identity, after verifying the owned service stopped.

        The caller must separately recover any user-mapped runtime files first;
        permission/mount failures leave this directory for explicit recovery.
        This does not release a database reservation or prove GPU recovery.
        """
        self.check()
        state = self.service.inspect()
        if (
            state is not None
            and (state["ActiveState"] not in {"inactive", "failed"} or state["MainPID"] != "0")
        ) or self.service.populated():
            raise SandboxUnavailable("Stop the owned task before removing its workspace")
        self.check()
        if not shutil.rmtree.avoids_symlink_attacks:
            raise SandboxUnavailable("Descriptor-based workspace cleanup is required")
        if any(path.is_relative_to(self.path.resolve()) for path in mounted_paths()):
            raise SandboxUnavailable("Unmount the private runtime before removing its workspace")
        # Retain identity on a partial failure so a replacement controller can
        # still identify the remaining files. Never follow nested symlinks.
        for name in os.listdir(self._fd):
            if name == "identity.json":
                continue
            info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=self._fd)
            else:
                os.unlink(name, dir_fd=self._fd)
        self.check()
        os.unlink("identity.json", dir_fd=self._fd)
        os.rmdir(self.name, dir_fd=self._root_fd)
        self.close()

    def close(self):
        for name in ("_fd", "_root_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        # Losing a Python handle is not proof that service or GPU stopped.
        self.close()


def prepare_workload_cgroup():
    """Called only inside the trusted, delegated RootlessKit namespace."""
    if os.getuid() != 0 or Path("/proc/self/cgroup").read_text().strip() != "0::/manager":
        raise SandboxUnavailable("Expected the private controller cgroup namespace")
    cg = Path("/sys/fs/cgroup")
    controllers = set((cg / "cgroup.controllers").read_text().split())
    required = {"cpu", "memory", "pids"}
    if not required <= controllers:
        raise SandboxUnavailable("Missing delegated workload controllers")
    enabled = " ".join("+" + name for name in sorted(required))
    (cg / "cgroup.subtree_control").write_text(enabled)
    workloads = cg / "workloads"
    workloads.mkdir()  # Existing workload state must be recovered, never reused.
    (workloads / "cgroup.subtree_control").write_text(enabled)
    for name, expected in WORKLOAD_LIMITS.items():
        (workloads / name).write_text(expected)
        if (workloads / name).read_text().strip() != expected:
            raise SandboxUnavailable("Workload parent cgroup limits were not applied")
    return dict(WORKLOAD_LIMITS)
