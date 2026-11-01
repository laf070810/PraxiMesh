"""Private local compilation through the existing device-free runsc task.

This module never identifies a GPU, executes PTX, or creates formal results.
The logical lock UUID identifies one compiler workspace, not a physical device.
"""

import hashlib
import json
import os
import signal
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from judge.container_compilation import CompilationCancelled, CompilationTask
from judge.container_compile_protocol import request
from judge.container_contract import MAX_SECONDS, PYTHON, WORKLOAD_PROFILE
from judge.container_journal import ContainerJournal, private
from judge.container_service import IMAGE_ID, LocalEngine
from judge.gpu_device_lock import DeviceLock
from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

SCOPE = "praximesh-local-cuda-compiler-v1"
COMMAND = [PYTHON, "-I", "/opt/praximesh/container_compile_entry.py", "normal"]
MAX_BINDING_BYTES = 8192


class LocalCompilerError(ValueError):
    """A bounded operator-facing workspace error."""


def inode(info):
    return [info.st_dev, info.st_ino]


def binding_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@contextmanager
def deferred_signals():
    stopped = False

    def stop(_number, _frame):
        nonlocal stopped
        stopped = True

    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)

        def check():
            if stopped:
                raise CompilationCancelled("Local compilation was cancelled")

        yield check
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class CompilerWorkspace:
    def __init__(self, directory, engine, *, create):
        self.directory = Path(directory)
        if (
            os.getuid() == 0
            or not self.directory.is_absolute()
            or ".." in self.directory.parts
            or self.directory.resolve() != self.directory
        ):
            raise LocalCompilerError(
                "Use an absolute, non-symlink compiler state path as non-root."
            )
        self.engine, self.create = engine, create
        self.stack = ExitStack()
        self.fds = {}
        self.marker = self.journal = None

    def read(self, name, directory="state"):
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.fds[directory],
        )
        with os.fdopen(descriptor, "rb") as stream:
            private(os.fstat(stream.fileno()))
            data = stream.read(MAX_BINDING_BYTES + 1)
        if len(data) > MAX_BINDING_BYTES:
            raise LocalCompilerError("Compiler state binding exceeded its size limit.")
        return data

    def directories(self):
        values = {}
        for name, descriptor in self.fds.items():
            opened = os.fstat(descriptor)
            named = (
                self.directory.lstat()
                if name == "state"
                else os.stat(name, dir_fd=self.fds["state"], follow_symlinks=False)
            )
            private(opened, directory=True)
            private(named, directory=True)
            if inode(opened) != inode(named):
                raise LocalCompilerError("Compiler state directory was replaced; retain it.")
            values[name] = inode(opened)
        return values

    def __enter__(self):
        try:
            self.engine.verify()
            created = False
            if self.create:
                try:
                    self.directory.mkdir(mode=0o700)
                    created = True
                except FileExistsError:
                    pass
            self.fds["state"] = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            self.stack.callback(os.close, self.fds["state"])
            private(os.fstat(self.fds["state"]), directory=True)
            for name in ("locks", "journal"):
                if created:
                    os.mkdir(name, mode=0o700, dir_fd=self.fds["state"])
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self.fds["state"],
                )
                self.fds[name] = descriptor
                self.stack.callback(os.close, descriptor)
            bindings = self.directories()
            self.slot = "GPU-" + str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{SCOPE}:{os.getuid()}:{bindings['state']}")
            )
            expected = {
                "version": 1,
                "scope": SCOPE,
                "uid": os.getuid(),
                "directories": bindings,
                "slot": self.slot,
            }
            if not created:
                self.marker = self.read("compiler.json")
                installation = decode_frame(self.read("installation.json", "journal"))
                if self.marker != binding_bytes({**expected, "installation": installation}):
                    raise LocalCompilerError(
                        "Compiler workspace binding changed; retain its state."
                    )
            self.guard = self.stack.enter_context(DeviceLock(self.directory / "locks", self.slot))
            self.journal = self.stack.enter_context(
                ContainerJournal(self.directory / "journal", self.guard, self.engine)
            )
            expected["installation"] = self.journal.identity
            if created:
                self.marker = binding_bytes(expected)
                descriptor = os.open(
                    "compiler.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=self.fds["state"],
                )
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(self.marker)
                    stream.flush()
                    os.fsync(stream.fileno())
                for descriptor in self.fds.values():
                    os.fsync(descriptor)
                parent = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            elif self.marker != binding_bytes(expected):
                raise LocalCompilerError(
                    "Compiler installation identity changed; retain its state."
                )
            self.binding = expected
            self.check()
            return self
        except BaseException:
            self.stack.close()
            raise

    def check(self):
        self.guard.assert_held()
        self.engine.check_socket()
        if (
            self.directories() != self.binding["directories"]
            or self.read("compiler.json") != self.marker
            or self.journal.read("installation.json") != self.binding["installation"]
        ):
            raise LocalCompilerError("Compiler workspace changed during this operation.")

    def record(self):
        self.check()
        record = self.journal.load()
        if record is not None and (
            record["version"] != 2
            or record["resource_profile"] != WORKLOAD_PROFILE
            or record["purpose"] != "task"
            or record["arguments"][4:] != COMMAND
        ):
            raise LocalCompilerError("This state does not describe a device-free compilation task.")
        return record

    def __exit__(self, *_error):
        self.stack.close()


def compile_source(source, operation, architecture, *, state, socket, image):
    message = request(source, operation, architecture)
    if not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
        raise LocalCompilerError("Use the immutable sha256 ID of a verified compiler image.")
    engine = LocalEngine(socket)
    with deferred_signals() as cancelled:
        with CompilerWorkspace(state, engine, create=True) as workspace:
            if workspace.record() is not None:
                raise LocalCompilerError("A previous compilation needs recover-compiler first.")
            deadline = time.monotonic() + MAX_SECONDS

            def check():
                cancelled()
                workspace.check()
                if time.monotonic() >= deadline:
                    raise SandboxUnavailable("The local compilation deadline expired")

            check()
            task = CompilationTask(workspace.journal, uuid.uuid4(), uuid.uuid4(), image=image)
            result = task.run(message, deadline=deadline, on_begin=check, on_tick=check)
            check()
            if workspace.record() is not None:
                raise SandboxUnavailable("Local compilation cleanup is incomplete")
    return {
        "scope": "Local CUDA compilation only; public examples were not executed.",
        "formal_result": False,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        **result,
    }


def recover_compiler(*, state, socket):
    engine = LocalEngine(socket)
    with deferred_signals() as cancelled:
        with CompilerWorkspace(state, engine, create=False) as workspace:
            record = workspace.record()
            cancelled()
            if record is not None:
                cleanup = workspace.journal.recover()
                if (
                    cleanup is None
                    or cleanup.get("removed") is not True
                    or cleanup.get("ready") is not False
                ):
                    raise SandboxUnavailable("Original compilation cleanup was not confirmed")
            if workspace.record() is not None:
                raise SandboxUnavailable("Original compilation record remains active")
    return {
        "scope": "Original local compiler task cleanup only.",
        "status": "recovered" if record is not None else "idle",
        "formal_result": False,
        "executed": False,
        "ready": False,
    }
