"""Read-only host PID ownership for trusted containerized control code."""

import errno
import os
import re
import stat
import uuid
from copy import deepcopy
from pathlib import Path

from judge.container_compiler import CompilerContainerService
from judge.container_driver import DriverContainerService
from judge.container_gpu import GPUContainerService
from judge.container_journal import PersistentRuntimeService
from judge.sandbox import SandboxUnavailable

HOST_PROC = Path("/run/praximesh-host-proc")
MAX_PIDS = 4096
PID_OBSERVER_LABEL = "io.praximesh.controller.pid-observer"
PID_OBSERVER_PROFILE = "host-pid-v1"
HOST_PID_SECURITY_OPTIONS = ["no-new-privileges", "label=disable"]


def host_pid_security_options(info):
    """Never disable an active daemon SELinux policy to obtain host PID visibility.

    Docker automatically appends label=disable for host PID. This profile only
    accepts that setting when the rootless daemon has no SELinux confinement;
    built-in seccomp and no-new-privileges remain required.
    """
    options = info.get("SecurityOptions") if type(info) is dict else None
    if (
        type(options) is not list
        or any(type(item) is not str for item in options)
        or "name=rootless" not in options
        or "name=seccomp,profile=builtin" not in options
        or any(item.split(",", 1)[0] == "name=selinux" for item in options)
    ):
        raise SandboxUnavailable("Host PID observation cannot disable an active security policy")
    return list(HOST_PID_SECURITY_OPTIONS)


class ProcessDisappeared(SandboxUnavailable):
    """A missing/exited process, never evidence that its container or GPU is idle."""

    def __init__(self, message, *, identity=None):
        super().__init__(message)
        self.identity = deepcopy(identity)


def host_manifest():
    info = Path("/proc").stat()
    return {
        "version": 1,
        "root": [info.st_dev, info.st_ino],
        "boot": str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())),
    }


def validate_manifest(value):
    try:
        if (
            type(value) is not dict
            or set(value) != {"version", "root", "boot"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["root"]) is not list
            or len(value["root"]) != 2
            or any(type(i) is not int or i < 0 for i in value["root"])
            or str(uuid.UUID(value["boot"])) != value["boot"]
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SandboxUnavailable("Invalid host process view identity") from None
    return value


def readonly_mounts(text):
    roots = 0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10:
            raise SandboxUnavailable("Malformed process mount inventory")
        path = fields[4]
        if path != str(HOST_PROC) and not path.startswith(str(HOST_PROC) + "/"):
            continue
        split = fields.index("-")
        if "ro" not in fields[5].split(",") or any(
            item.startswith(("shared:", "master:", "propagate_from:")) for item in fields[6:split]
        ):
            raise SandboxUnavailable("Host process mounts must all be private and read-only")
        if path == str(HOST_PROC):
            roots += 1
            if fields[3] != "/" or fields[split + 1] != "proc":
                raise SandboxUnavailable("Expected the complete pinned procfs view")
    if roots != 1:
        raise SandboxUnavailable("Missing or shadowed host process mount")


def read_file(directory, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SandboxUnavailable("Unexpected process metadata type")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise SandboxUnavailable("Process metadata exceeded its bound")
    return raw.decode("utf-8", errors="strict")


def process_identity(pid, process_stat, status, cgroup):
    try:
        number, fields = process_stat.split(" (", 1)
        fields = fields.rsplit(")", 1)[1].split()
        nspids = [line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")]
        if (
            int(number) != pid
            or len(fields) < 20
            or not fields[19].isascii()
            or not fields[19].isdecimal()
            or int(fields[19]) <= 0
            or len(nspids) != 1
            or not nspids[0]
            or any(not n.isascii() or not n.isdecimal() or int(n) <= 0 for n in nspids[0])
            or int(nspids[0][0]) != pid
            or not re.fullmatch(r"0::/[a-zA-Z0-9/._\\:-]*\n?", cgroup)
        ):
            raise ValueError
        identity = {
            "pid": pid,
            "birth": int(fields[19]),
            "nspids": [int(n) for n in nspids[0]],
            "cgroup": cgroup.strip(),
        }
        if fields[0] in {"Z", "X", "x"}:
            raise ProcessDisappeared("Observed process has exited", identity=identity)
        return identity
    except (ValueError, IndexError, TypeError):
        raise SandboxUnavailable("Unusable process identity") from None


class HostProcessView:
    def __init__(self, saved):
        self.saved = deepcopy(validate_manifest(saved))
        self.fd = None
        try:
            self.fd = os.open(HOST_PROC, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.fd is None:
            raise SandboxUnavailable("Host process view is closed")
        try:
            for info in (os.fstat(self.fd), HOST_PROC.lstat()):
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or [info.st_dev, info.st_ino] != self.saved["root"]
                ):
                    raise SandboxUnavailable("Host process mount was replaced")
            if not os.fstatvfs(self.fd).f_flag & os.ST_RDONLY:
                raise SandboxUnavailable("Host process mount is writable")
            readonly_mounts(Path("/proc/self/mountinfo").read_text())
            boot = read_file(self.fd, "sys/kernel/random/boot_id", 64).strip()
            if boot != self.saved["boot"]:
                raise SandboxUnavailable("Host boot identity changed")
        except (OSError, ValueError, UnicodeError):
            raise SandboxUnavailable("Host process view cannot be verified") from None

    def read(self, pid):
        if type(pid) is not int or not 0 < pid < 2**31:
            raise SandboxUnavailable("A positive host PID is required")
        self.check()
        try:
            directory = os.open(
                str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd
            )
            try:
                before = read_file(directory, "stat", 4096)
                status = read_file(directory, "status", 16384)
                group = read_file(directory, "cgroup", 4096)
                after = read_file(directory, "stat", 4096)
                first = process_identity(pid, before, status, group)
                second = process_identity(
                    pid,
                    after,
                    read_file(directory, "status", 16384),
                    read_file(directory, "cgroup", 4096),
                )
                if first != second:
                    raise SandboxUnavailable("Process identity changed during observation")
            finally:
                os.close(directory)
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ESRCH}:
                raise ProcessDisappeared("Host process disappeared") from None
            raise SandboxUnavailable("Host process cannot be read") from None
        except (ValueError, UnicodeError):
            raise SandboxUnavailable("Host process cannot be read") from None
        self.check()
        return second

    def require_same_pid_namespace(self):
        """NVIDIA queries must see the complete pinned host PID domain.

        The kernel's fixed self link names this caller in the pinned procfs.
        Looking up numeric getpid() would instead read host PID 1 when called
        from a private container namespace, falsely accepting the host init.
        """
        self.check()
        pid = os.getpid()
        try:
            directory = os.open("self", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self.fd)
            try:

                def observed():
                    return process_identity(
                        pid,
                        read_file(directory, "stat", 4096),
                        read_file(directory, "status", 16384),
                        read_file(directory, "cgroup", 4096),
                    )

                first, second = observed(), observed()
                if first != second or first["nspids"] != [pid]:
                    raise SandboxUnavailable(
                        "NVIDIA observer must share the pinned host PID namespace"
                    )
            finally:
                os.close(directory)
        except (OSError, ValueError, UnicodeError):
            raise SandboxUnavailable("NVIDIA observer PID namespace cannot be verified") from None
        self.check()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class OwnedProcesses:
    """A live observation fence, never a device lease or readiness capability."""

    def __init__(self, control, service):
        if type(service) not in {
            PersistentRuntimeService,
            CompilerContainerService,
            DriverContainerService,
            GPUContainerService,
        }:
            raise SandboxUnavailable("Expected a reviewed persistent task service")
        if service.journal is not control.journal or control.host_processes is None:
            raise SandboxUnavailable("Task and observer must share the original controller")
        self.control, self.service, self.anchor = control, service, None
        self.record = deepcopy(control.journal.load())
        if self.record is None or self.record["phase"] != "running":
            raise SandboxUnavailable("Original running task record is required")
        self.anchor = self.check()

    def original_state(self):
        """Inspect the pinned startup without claiming live process ownership."""
        self.control.check()
        if self.control.journal.load() != self.record:
            raise SandboxUnavailable("Original process observation record changed")
        before = self.service.inspect()
        if (
            before is None
            or before["id"] != self.record["container_id"]
            or self.service.owner != self.record["owner"]
        ):
            raise SandboxUnavailable("Original task identity changed")
        if self.control.journal.load() != self.record:
            raise SandboxUnavailable("Original process observation record changed")
        self.control.check()
        return before

    def check(self):
        return self._check(allow_exit=False)

    def check_empty_termination(self):
        """Only after a full empty inventory during a triggered maintenance fault."""
        if self.record["purpose"] != "maintenance":
            raise SandboxUnavailable("Termination observation requires original maintenance")
        return self._check(allow_exit=True)

    def _check(self, *, allow_exit):
        def exited(state):
            return (
                allow_exit
                and state["running"] is False
                and state["status"] == "exited"
                and state["pid"] == 0
            )

        before = self.original_state()
        if not before["running"]:
            if exited(before):
                return None
            raise SandboxUnavailable("Original task is no longer running")
        try:
            main = self.control.host_processes.read(before["pid"])
        except ProcessDisappeared as error:
            # A procfs disappearance alone proves nothing. Only the unchanged
            # original runtime can confirm this task's completed exit.
            if (
                error.identity is not None
                and self.anchor is not None
                and error.identity != self.anchor
            ):
                raise SandboxUnavailable("Exited process identity changed") from None
            if exited(self.original_state()):
                return None
            raise
        if (
            len(main["nspids"]) < 2
            or main["nspids"][-1] != 1
            or not main["cgroup"].endswith("/docker-" + before["id"] + ".scope")
            or self.anchor is not None
            and main != self.anchor
        ):
            raise SandboxUnavailable("Task PID 1 or cgroup identity changed")
        after = self.original_state()
        child_oom = (
            before["running"] is True
            and before["oom_killed"] is False
            and after == {**before, "oom_killed": True}
        )
        # Docker also records a child cgroup OOM while the original PID 1 lives.
        # Preserve that real flag; it is not a new process/startup identity.
        if after != before and not child_oom:
            if exited(after):
                return None
            raise SandboxUnavailable("Task changed during process observation")
        self.control.check()
        return main

    def require_owned(self, pids):
        if (
            type(pids) is not list
            or len(pids) > MAX_PIDS
            or any(type(pid) is not int or not 0 < pid < 2**31 for pid in pids)
            or len(set(pids)) != len(pids)
        ):
            raise SandboxUnavailable("Invalid host process inventory")
        main = self.check()
        found = [self.control.host_processes.read(pid) for pid in pids]
        if any(item["cgroup"] != main["cgroup"] for item in found):
            raise SandboxUnavailable("An observed host process is outside the original task")
        # Re-read after the complete inventory, so a reused PID cannot inherit a
        # prior record's ownership within this observation.
        if [self.control.host_processes.read(pid) for pid in pids] != found:
            raise SandboxUnavailable("Observed process identity changed")
        self.check()
        return found
