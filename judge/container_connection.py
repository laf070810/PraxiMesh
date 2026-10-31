"""Host-side connection to a held, verified trusted runtime container."""

import os
import socket
import subprocess
import tempfile
import time

from judge.container_compiler import CompilerContainerService
from judge.container_contract import PYTHON
from judge.container_journal import PersistentRuntimeService
from judge.container_runtime_service import RuntimeContainerService
from judge.container_transport import BRIDGE, hello
from judge.gpu_transport import Channel
from judge.sandbox import SandboxUnavailable


class ContainerConnection:
    def __init__(self, service, status, *, on_tick):
        from judge.container_gpu import GPUContainerService

        if type(service) not in {
            RuntimeContainerService,
            PersistentRuntimeService,
            CompilerContainerService,
            GPUContainerService,
        }:
            raise ValueError("A verified runtime container is required")
        self.service, self.on_tick = service, on_tick
        self.channel = self.process = self.config = None
        self.closed = False
        self.finishing = False
        state = service.inspect()
        if (
            state is None
            or not state["running"]
            or status["nonce"] != state["arguments"][2]
            or status["deadline"] != float(state["arguments"][1])
            or time.monotonic() >= status["deadline"]
        ):
            raise SandboxUnavailable("Control transport requires the original live invocation")
        self.identity = state["id"], tuple(state["arguments"])
        self.deadline = status["deadline"]
        local, remote = socket.socketpair()
        try:
            self.tick()
            self.config = tempfile.TemporaryDirectory(prefix="px-control-client-")
            self.process = subprocess.Popen(
                [
                    "docker",
                    "--config",
                    self.config.name,
                    "--host",
                    "unix://" + str(service.engine.path),
                    "container",
                    "exec",
                    "--interactive",
                    self.identity[0],
                    PYTHON,
                    "-I",
                    BRIDGE,
                ],
                stdin=remote,
                stdout=remote,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                start_new_session=True,
            )
            remote.close()
            self.channel = Channel(local, deadline=self.deadline, on_tick=self.tick)
            if self.channel.receive() != hello(status):
                raise SandboxUnavailable("Container control startup identity mismatch")
            self.channel.send({"event": "transport-accepted", "nonce": status["nonce"]})
        except BaseException:
            local.close()
            remote.close()
            self.close()
            raise

    def tick(self):
        self.service.guard.assert_held()
        self.service.engine.check_socket()
        self.on_tick()
        state = self.service.inspect()
        if (
            state is None
            or (not state["running"] and not self.finishing)
            or (state["id"], tuple(state["arguments"])) != self.identity
            or time.monotonic() >= self.deadline
        ):
            raise SandboxUnavailable("Original control container is no longer live")

    def finish(self):
        self.finishing = True
        self.channel.expect_eof()
        try:
            self.process.wait(timeout=max(0.01, min(3, self.deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            raise SandboxUnavailable("Container bridge did not finish") from None
        if self.process.returncode != 0:
            raise SandboxUnavailable("Container bridge did not complete cleanly")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.channel is not None:
            try:
                self.channel.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.channel.close()
        if self.process is not None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()  # Only this owned CLI; not proof of guest termination.
                self.process.wait(timeout=3)
        if self.config is not None:
            self.config.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
