"""Private, bounded transport between two trusted controllers, never the guest."""

import json
import os
import select
import socket
import struct
import tempfile
import time
from pathlib import Path

from judge.gpu_session import decode_frame
from judge.sandbox import SandboxUnavailable

FRAME_BYTES = 4 * 1024 * 1024
TOTAL_BYTES = 8 * 1024 * 1024


def process_cgroup(pid):
    if type(pid) is not int or pid <= 0:
        raise SandboxUnavailable("GPU process identity is unavailable")
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        raise SandboxUnavailable("GPU process ownership could not be read") from None
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise SandboxUnavailable("GPU process cgroup is ambiguous")
    return lines[0][3:]


def belongs_to(group, expected):
    return group == expected or group.startswith(expected + "/")


def authorize_peer(sock, service):
    pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    state = service.inspect()
    if (
        uid != os.getuid()
        or state is None
        or not belongs_to(process_cgroup(pid), service.cgroup_name)
    ):
        raise SandboxUnavailable("Control connection does not belong to the owned task service")
    return pid


class Channel:
    def __init__(self, sock, *, deadline, on_tick):
        self.sock = sock
        self.sock.setblocking(False)
        self.deadline, self.on_tick = deadline, on_tick
        self.next_tick = 0
        self.sent = self.received = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise SandboxUnavailable("GPU controller transport deadline expired")
        if time.monotonic() >= self.next_tick:
            self.on_tick()
            self.next_tick = time.monotonic() + 0.25
            if time.monotonic() >= self.deadline:
                raise SandboxUnavailable("GPU controller transport deadline expired")

    def _transfer(self, size=None, data=None):
        output = bytearray()
        offset = 0
        writing = data is not None
        count = len(data) if writing else size
        while offset < count:
            self.check()
            readable, writable, _ = select.select(
                [] if writing else [self.sock], [self.sock] if writing else [], [], 0.05
            )
            if not (readable or writable):
                continue
            try:
                if writing:
                    changed = self.sock.send(data[offset : offset + 65536])
                else:
                    chunk = self.sock.recv(min(65536, count - offset))
                    changed = len(chunk)
                    output.extend(chunk)
            except BlockingIOError:
                continue
            except OSError:
                raise SandboxUnavailable("GPU controller connection failed") from None
            if not changed:
                raise SandboxUnavailable("GPU controller disconnected before completion")
            offset += changed
        return bytes(output)

    def send(self, value):
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if not 0 < len(encoded) <= FRAME_BYTES or self.sent + len(encoded) > TOTAL_BYTES:
            raise SandboxUnavailable("GPU controller message exceeds its budget")
        self.sent += len(encoded)
        self._transfer(data=struct.pack("!I", len(encoded)) + encoded)

    def receive(self):
        size = struct.unpack("!I", self._transfer(size=4))[0]
        if not 0 < size <= FRAME_BYTES or self.received + size > TOTAL_BYTES:
            raise SandboxUnavailable("GPU controller message exceeds its budget")
        self.received += size
        return decode_frame(self._transfer(size=size))

    def connected(self):
        self.check()
        try:
            data = self.sock.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return
        except OSError:
            raise SandboxUnavailable("GPU worker connection failed") from None
        if data:
            raise SandboxUnavailable("Unexpected message during GPU execution")
        raise SandboxUnavailable("GPU worker disconnected; stop its owned workload")

    def expect_eof(self):
        while True:
            self.check()
            if select.select([self.sock], [], [], 0.05)[0]:
                try:
                    data = self.sock.recv(1)
                except OSError:
                    raise SandboxUnavailable(
                        "GPU controller connection failed before clean completion"
                    ) from None
                if data:
                    raise SandboxUnavailable("Unexpected trailing controller output")
                return

    def close(self):
        self.sock.close()


class PrivateListener:
    def __init__(self):
        # A short pathname also works across the private network namespace.
        # Abstract sockets would be scoped to that namespace instead.
        self.directory = tempfile.TemporaryDirectory(prefix="px-gpu-ipc-", dir="/tmp")
        self.path = Path(self.directory.name) / "control.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.bind(str(self.path))
            self.path.chmod(0o600)
            self.sock.listen(2)
            self.sock.setblocking(False)
        except BaseException:
            self.close()
            raise

    def accept(self, service, *, deadline, on_tick):
        while True:
            on_tick()
            if time.monotonic() >= deadline:
                raise SandboxUnavailable("GPU controller did not connect before its deadline")
            if select.select([self.sock], [], [], 0.1)[0]:
                connection, _address = self.sock.accept()
                try:
                    authorize_peer(connection, service)
                    return Channel(connection, deadline=deadline, on_tick=on_tick)
                except BaseException:
                    connection.close()
                    raise

    def close(self):
        self.sock.close()
        self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()
