"""Bounded commands for trusted private-runtime control, never host learner code."""

import math
import os
import selectors
import subprocess
import time

from judge.sandbox import SandboxUnavailable

MAX_OUTPUT = 1024 * 1024
MAX_ERROR = 64 * 1024


class CommandFailure(SandboxUnavailable):
    def __init__(self, message, *, returncode=None, diagnostics=b""):
        # Raw output is available to fixed infrastructure acceptance only. It
        # never becomes an exception message, learner result or ordinary log.
        super().__init__(message)
        self.returncode = returncode
        self.diagnostics = diagnostics


def run_command(
    command, *, env, deadline, on_tick, seconds=10, data=b"", max_output=MAX_OUTPUT, check=True
):
    if (
        not math.isfinite(seconds)
        or not 0 < seconds <= 90
        or not math.isfinite(deadline)
        or type(data) is not bytes
        or len(data) > MAX_OUTPUT
        or type(max_output) is not int
        or not 0 < max_output <= MAX_OUTPUT
    ):
        raise ValueError("Invalid private command budget")
    until = min(deadline, time.monotonic() + seconds)
    next_tick = 0

    def tick():
        nonlocal next_tick
        if time.monotonic() >= until:
            raise CommandFailure("Private runtime command exceeded its deadline")
        if time.monotonic() >= next_tick:
            on_tick()
            next_tick = time.monotonic() + 0.25
            if time.monotonic() >= until:
                raise CommandFailure("Private runtime command exceeded its deadline")

    tick()
    try:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        raise CommandFailure("Private runtime command could not start") from None
    output, errors = bytearray(), bytearray()
    sent = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            if data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            else:
                process.stdin.close()
            while selector.get_map() or process.poll() is None:
                tick()
                for key, _events in selector.select(0.05):
                    if key.data == "in":
                        try:
                            sent += os.write(key.fd, data[sent : sent + 8192])
                        except BrokenPipeError:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        if sent == len(data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = output if key.data == "out" else errors
                        target.extend(chunk)
                        if len(target) > (max_output if key.data == "out" else MAX_ERROR):
                            raise CommandFailure(
                                "Private runtime command exceeded its output budget"
                            )
            tick()
        if check and (process.returncode or sent != len(data)):
            raise CommandFailure(
                "Private runtime command failed",
                returncode=process.returncode,
                diagnostics=bytes(errors),
            )
        return subprocess.CompletedProcess(
            command, process.returncode, bytes(output), bytes(errors)
        )
    finally:
        try:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise CommandFailure("Private runtime command did not terminate") from None
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
