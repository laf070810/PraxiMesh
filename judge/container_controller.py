"""Verified host mounts for a trusted, rootless container control endpoint.

This owns no GPU admission, database reservation or learner execution entry.
"""

import hashlib
import os
import uuid
from pathlib import Path

from judge.container_compiler import SOURCES as COMPILER_SOURCES
from judge.container_driver import SOURCES as DRIVER_SOURCES
from judge.container_journal import ContainerJournal, private
from judge.container_monitor import check_prerequisites, validate_observer
from judge.container_processes import HostProcessView, host_manifest, validate_manifest
from judge.container_runtime_service import ROOT
from judge.container_service import LocalEngine
from judge.gpu_device_lock import DeviceLock
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

STATE = Path("/var/lib/praximesh-control")
SOCKET = Path("/run/praximesh-engine/docker.sock")
CLI = Path("/usr/local/bin/docker")
CLI_VERSION = "29.9.2"
CLI_ARCHIVE_SHA256 = "9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb"
CLI_SHA256 = "6c71cfe71faff949a602946a4df4799b563232e79d0b8b6fd343d0ef081c9621"
LIMITS = {
    "cpu.max": "100000 100000",
    "memory.max": "536870912",
    "memory.swap.max": "0",
    "pids.max": "128",
}
CONTROLLER_SOURCES = tuple(
    dict.fromkeys(
        (
            *COMPILER_SOURCES,
            *DRIVER_SOURCES,
            "judge/container_controller.py",
            "judge/container_processes.py",
            "judge/container_monitor.py",
            "judge/nvml_artifacts.py",
            "judge/nvml_reader.py",
            "judge/smi_inventory.py",
            "judge/container_compilation.py",
            "scripts/container_controller_fixture.py",
            "deploy/container-controller.Dockerfile",
            "deploy/container-monitor.fragment",
            "scripts/build_container_monitor.py",
            "vendor/docker-cli/LICENSE",
            "vendor/docker-cli/NOTICE",
        )
    )
)


def controller_digest():
    digest = hashlib.sha256()
    for name in CONTROLLER_SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def manifest(directory, engine, *, host_processes=False, device_observer=None):
    """Host prepares a private mount identity; does not mount or change ownership."""
    if os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("A non-root operator is required")
    for path in (directory, directory / "locks", directory / "journal"):
        private(path.lstat(), directory=True)
    engine.verify()
    if type(host_processes) is not bool:
        raise ValueError("Explicit process observation selection is required")
    if device_observer is not None:
        if not host_processes:
            raise ValueError("NVIDIA observation requires the pinned host process view")
        validate_observer(device_observer)
    return {
        "version": device_observer["schema"] + 2
        if device_observer is not None
        else 2
        if host_processes
        else 1,
        "operator": [os.getuid(), os.getgid()],
        "root": identity(directory),
        "locks": identity(directory / "locks"),
        "journal": identity(directory / "journal"),
        "socket": identity(engine.path),
        "engine": str(uuid.UUID(engine.run("info", "--format", "{{.ID}}").stdout.decode().strip())),
        **({"host_processes": host_manifest()} if host_processes else {}),
        **({"device_observer": device_observer} if device_observer is not None else {}),
    }


def mapped_zero(data):
    try:
        rows = [tuple(map(int, row.split())) for row in data.splitlines()]
        if any(len(row) != 3 or min(row) < 0 or row[2] == 0 for row in rows):
            raise ValueError
        zero = [row for row in rows if row[0] == 0]
        if len(zero) != 1 or zero[0][1] == 0 or zero[0][2] != 1:
            raise ValueError
        return zero[0][1]
    except (ValueError, TypeError):
        raise SandboxUnavailable("Expected rootless operator mapping is unavailable") from None


def environment(operator):
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    uid = mapped_zero(Path("/proc/self/uid_map").read_text())
    gid = mapped_zero(Path("/proc/self/gid_map").read_text())
    if (
        os.getuid() != 0
        or os.getgid() != 0
        or [uid, gid] != operator
        or any(
            int(status[key].strip(), 16)
            for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        or status["NoNewPrivs"].strip() != "1"
        or Path("/proc/self/cgroup").read_text().strip() != "0::/"
        or not os.statvfs("/").f_flag & os.ST_RDONLY
    ):
        raise SandboxUnavailable(
            "Controller requires the original rootless operator and containment"
        )
    if hashlib.sha256(CLI.read_bytes()).hexdigest() != CLI_SHA256:
        raise SandboxUnavailable("Controller Docker CLI identity changed")
    if (ROOT / "controller-contract").read_text().strip() != controller_digest():
        raise SandboxUnavailable("Controller source identity changed")
    limits = {name: (Path("/sys/fs/cgroup") / name).read_text().strip() for name in LIMITS}
    if limits != LIMITS:
        raise SandboxUnavailable("Controller actual cgroup limits differ from the contract")
    return {"uid": os.getuid(), "gid": os.getgid(), "mapped_operator": [uid, gid], "limits": limits}


class ContainerController:
    def __init__(self, device):
        self.guard = self.journal = self.engine = self.host_processes = None
        self.saved = self.read_manifest()
        expected = {"version", "operator", "root", "locks", "journal", "socket", "engine"}
        version = self.saved.get("version")
        if type(version) is not int or version not in {1, 2, 3, 4}:
            raise SandboxUnavailable("Unknown controller mount contract")
        if version >= 2:
            expected.add("host_processes")
        if version >= 3:
            expected.add("device_observer")
        if set(self.saved) != expected:
            raise SandboxUnavailable("Unknown controller mount contract")
        if version >= 2:
            validate_manifest(self.saved["host_processes"])
        if version >= 3:
            validate_observer(self.saved["device_observer"])
            if version != self.saved["device_observer"]["schema"] + 2:
                raise SandboxUnavailable("NVIDIA observer and controller profiles differ")
            if self.saved["device_observer"]["target"]["uuid"] != device:
                raise SandboxUnavailable("Controller and NVIDIA observer select different devices")
        for key in ("operator", "root", "locks", "journal", "socket"):
            value = self.saved[key]
            if (
                type(value) is not list
                or len(value) != 2
                or any(type(i) is not int or i < 0 for i in value)
            ):
                raise SandboxUnavailable("Invalid controller mount identity")
        try:
            if str(uuid.UUID(self.saved["engine"])) != self.saved["engine"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SandboxUnavailable("Invalid controller engine identity") from None
        self.process_identity = environment(self.saved["operator"])
        try:
            if version >= 2:
                self.host_processes = HostProcessView(self.saved["host_processes"])
            self.check_mounts()
            self.engine = LocalEngine(SOCKET)
            self.guard = DeviceLock(STATE / "locks", device).acquire()
            self.journal = ContainerJournal(STATE / "journal", self.guard, self.engine)
            if self.journal.identity["engine"] != self.saved["engine"]:
                raise SandboxUnavailable("Controller daemon identity changed")
            self.check()
        except BaseException:
            self.close()
            raise

    def read_manifest(self):
        private(STATE.lstat(), directory=True)
        fd = os.open(STATE / "mounts.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            private(os.fstat(source.fileno()))
            value = decode_frame(source.read(8193))
            if source.tell() > 8192 or type(value) is not dict:
                raise SandboxUnavailable("Controller mount manifest is invalid")
            return value

    def check_mounts(self):
        if self.read_manifest() != self.saved:
            raise SandboxUnavailable("Controller mount manifest changed")
        for key, path in (
            ("root", STATE),
            ("locks", STATE / "locks"),
            ("journal", STATE / "journal"),
        ):
            private(path.lstat(), directory=True)
            if identity(path) != self.saved[key]:
                raise SandboxUnavailable("Controller state mount changed")
        if identity(SOCKET) != self.saved["socket"]:
            raise SandboxUnavailable("Controller socket mount changed")
        if self.host_processes is not None:
            self.host_processes.check()
        if self.saved["version"] >= 3:
            check_prerequisites(self.saved["device_observer"], self.host_processes)

    def check(self):
        self.check_mounts()
        self.guard.assert_held()
        self.engine.check_socket()
        self.journal.check()

    def close(self):
        if self.host_processes is not None:
            self.host_processes.close()
        if self.journal is not None:
            self.journal.close()
        if self.guard is not None:
            self.guard.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
