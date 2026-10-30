"""Bounded controller transport for an already prepared gVisor CUDA container.

Only the trusted isolation controller constructs this object with its attached
process and owned-container cleanup function. This module neither creates an
arbitrary process nor authorizes GPU admission. Expected values stay outside
the container; no guest-reported time or verdict is accepted.
"""

import json
import os
import selectors
import subprocess
import time

from judge.gpu_contract import batch_request, case_request, compile_request
from judge.gpu_policy import PROFILE
from judge.sandbox import SandboxUnavailable


class GPUExecutionError(Exception):
    def __init__(self, verdict):
        super().__init__(verdict)
        self.verdict = verdict


def decode_frame(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate protocol key")
            value[key] = item
        return value

    def constant(_value):
        raise ValueError("Nonfinite protocol number")

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, RecursionError, UnicodeError):
        raise SandboxUnavailable("GPU driver emitted an invalid protocol frame") from None
    if type(value) is not dict:
        raise SandboxUnavailable("GPU driver frame must be an object")
    return value


class GPUSession:
    def __init__(self, process, cleanup, *, architecture, mode, on_tick, exit_verdict=None):
        if mode not in {"run", "submit"}:
            raise ValueError("Unknown GPU execution mode")
        self.process = process
        self.cleanup = cleanup
        self.on_tick = on_tick
        self.exit_verdict = exit_verdict or (lambda _code: None)
        self.mode = mode
        self.architecture = list(architecture)
        self.started = time.monotonic()
        self.deadline = self.started + PROFILE[mode + "_seconds"]
        self.next_tick = self.started
        self.compiled = False
        self.closed = False
        self.buffer = bytearray()
        self.stdout_bytes = self.stderr_bytes = 0
        self.selector = selectors.DefaultSelector()
        for stream, kind in ((process.stdout, "out"), (process.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, kind)
        os.set_blocking(process.stdin.fileno(), False)

    def _check_time(self, deadline):
        now = time.monotonic()
        if now >= self.deadline or now >= deadline:
            raise GPUExecutionError("time_limit")
        if now >= self.next_tick:
            self.on_tick()
            self.next_tick = time.monotonic() + PROFILE["heartbeat_seconds"]
            # Lease callbacks can wait for database locks. Account for that time.
            if time.monotonic() >= min(self.deadline, deadline):
                raise GPUExecutionError("time_limit")

    def _exited(self):
        code = self.process.poll()
        if code is None:
            return False
        verdict = {2: "runtime_error", 3: "syntax_error", 4: "memory_limit"}.get(code)
        if verdict is None and self.exit_verdict(code) == "memory_limit":
            # Only a trusted container/cgroup observer can identify an OOM;
            # SIGKILL or a diagnostic string alone is insufficient evidence.
            verdict = "memory_limit"
        if verdict:
            raise GPUExecutionError(verdict)
        raise SandboxUnavailable("GPU driver exited before completing its protocol")

    def _read(self, key):
        data = os.read(key.fd, 8192)
        if not data:
            self.selector.unregister(key.fileobj)
            return
        if key.data == "err":
            self.stderr_bytes += len(data)
            if self.stderr_bytes > PROFILE["stderr_bytes"]:
                raise GPUExecutionError("output_limit")
            return  # Never persist or return raw compiler/device diagnostics.
        self.stdout_bytes += len(data)
        if self.stdout_bytes > PROFILE["stdout_bytes"]:
            raise GPUExecutionError("output_limit")
        self.buffer.extend(data)
        if len(self.buffer) > PROFILE["frame_bytes"]:
            raise GPUExecutionError("output_limit")

    def exchange(self, request, *, seconds):
        if self.closed:
            raise SandboxUnavailable("GPU session is already closed")
        if self.buffer:
            raise SandboxUnavailable("Unexpected GPU driver output between requests")
        encoded = (json.dumps(request, allow_nan=False) + "\n").encode()
        if len(encoded) > PROFILE["frame_bytes"]:
            raise ValueError("GPU request exceeds the protocol limit")
        started = time.monotonic_ns()
        deadline = time.monotonic() + seconds
        sent = 0
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "in")
        try:
            while True:
                self._check_time(deadline)
                for key, _events in self.selector.select(timeout=0.05):
                    if key.data == "in":
                        try:
                            sent += os.write(key.fd, encoded[sent : sent + 8192])
                        except BrokenPipeError:
                            self.selector.unregister(key.fileobj)
                            continue
                        if sent == len(encoded):
                            self.selector.unregister(key.fileobj)
                    else:
                        self._read(key)
                if b"\n" in self.buffer:
                    raw, remainder = self.buffer.split(b"\n", 1)
                    self.buffer.clear()
                    if sent != len(encoded) or remainder:
                        raise SandboxUnavailable("GPU driver protocol order changed")
                    elapsed_ms = (time.monotonic_ns() - started) / 1e6
                    return decode_frame(raw), elapsed_ms
                self._exited()
        finally:
            try:
                self.selector.unregister(self.process.stdin)
            except KeyError:
                pass

    def compile(self, source, operation):
        if self.compiled:
            raise SandboxUnavailable("GPU session cannot compile a second source")
        frame, _elapsed = self.exchange(
            compile_request(source, operation), seconds=PROFILE["compile_seconds"]
        )
        if (
            set(frame) != {"event", "architecture"}
            or frame["event"] != "compiled"
            or type(frame["architecture"]) is not list
            or any(type(value) is not int for value in frame["architecture"])
            or frame["architecture"] != self.architecture
        ):
            raise SandboxUnavailable("GPU compilation identity differs from the selected device")
        self.operation = operation
        self.compiled = True
        return (time.monotonic() - self.started) * 1000

    def execute(self, case, *, batch=False):
        if not self.compiled:
            raise SandboxUnavailable("GPU source has not compiled")
        if type(batch) is not bool:
            raise ValueError("GPU batch selection must be explicit")
        request = (batch_request if batch else case_request)(self.operation, case)
        frame, elapsed_ms = self.exchange(request, seconds=PROFILE["request_seconds"])
        if set(frame) != {"values"} or type(frame["values"]) is not list:
            raise SandboxUnavailable("GPU execution returned an unexpected frame")
        return frame["values"], elapsed_ms

    def finish(self):
        """EOF is a normal driver shutdown; verify exit and no unsolicited output."""
        if self.closed:
            raise SandboxUnavailable("GPU session is already closed")
        self.process.stdin.close()
        deadline = time.monotonic() + PROFILE["request_seconds"]
        while self.selector.get_map() or self.process.poll() is None:
            self._check_time(deadline)
            for key, _events in self.selector.select(timeout=0.05):
                self._read(key)
            if self.buffer:
                raise SandboxUnavailable("GPU driver emitted unsolicited trailing output")
        if self.process.returncode != 0:
            self._exited()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            # Always inspect/stop/wait for the owned container. A detached or
            # exited CLI is insufficient evidence that GPU work has stopped.
            self.cleanup()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise SandboxUnavailable("GPU attach process did not terminate") from None
        finally:
            self.selector.close()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()
