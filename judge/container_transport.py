"""Bounded IPC inside a trusted task container, never exposed to its guest."""

import json
import math
import os
import re
import select
import socket
import stat
import struct
import time
from pathlib import Path

from judge.container_contract import MAX_SECONDS, PROTOCOL, PYTHON, STATE, STATUS_BYTES
from judge.gpu_transport import TOTAL_BYTES, Channel
from judge.sandbox import SandboxUnavailable

TRANSPORT = "praximesh-container-control-v1"
BRIDGE = "/opt/praximesh/judge/container_bridge.py"
SOCKET = STATE + "/control.sock"
BUFFER_BYTES = 65536
# Channel charges payload bytes; a nonempty frame adds four header bytes.
WIRE_BYTES = 5 * TOTAL_BYTES


def read_status():
    descriptor = os.open(
        STATE + "/status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 65532
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > STATUS_BYTES
        ):
            raise SandboxUnavailable("Invalid private supervision state")
        return json.loads(source.read(STATUS_BYTES + 1))


def readiness():
    if (
        os.getuid() != 65532
        or os.getgid() != 65532
        or Path("/proc/self/cgroup").read_text().strip() != "0::/"
        or not os.statvfs("/").f_flag & os.ST_RDONLY
    ):
        raise SandboxUnavailable("Expected a private trusted control container")
    end = time.monotonic() + 3
    while True:
        try:
            value = read_status()
            break
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= end:
                raise SandboxUnavailable("Supervisor did not publish its identity") from None
            time.sleep(0.02)
    try:
        if (
            value["protocol"] != PROTOCOL
            or type(value["child"]) is not int
            or value["child"] <= 1
            or type(value["nonce"]) is not str
            or not re.fullmatch("[a-f0-9]{32}", value["nonce"])
            or type(value["deadline"]) not in {int, float}
            or not math.isfinite(value["deadline"])
            or not time.monotonic() < value["deadline"] <= time.monotonic() + MAX_SECONDS
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SandboxUnavailable("Invalid current supervision identity") from None
    return value


def hello(status):
    return {
        "event": "transport-ready",
        "protocol": TRANSPORT,
        **{key: status[key] for key in ("nonce", "child", "deadline")},
    }


def peer(sock, *, child=None):
    pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != 65532 or gid != 65532 or pid <= 1:
        raise SandboxUnavailable("Unexpected private control peer")
    try:
        group = Path(f"/proc/{pid}/cgroup").read_text().strip()
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]
        if group != "0::/" or (
            pid != child
            if child is not None
            else command != [PYTHON.encode(), b"-I", BRIDGE.encode()]
        ):
            raise ValueError
    except (OSError, ValueError):
        raise SandboxUnavailable(
            "Control peer is not the owned controller or fixed bridge"
        ) from None
    return pid


class ContainerListener:
    def __init__(self, *, on_tick):
        self.status = readiness()
        if self.status["child"] != os.getpid():
            raise SandboxUnavailable("Only the supervised main controller may listen")
        self.on_tick = on_tick
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.bind(SOCKET)  # Existing paths are never removed or reused.
            Path(SOCKET).chmod(0o600)
            self.sock.listen(1)
            self.sock.setblocking(False)
        except BaseException:
            self.close()
            raise

    def accept(self):
        try:
            while time.monotonic() < self.status["deadline"]:
                self.on_tick()
                if not select.select([self.sock], [], [], 0.05)[0]:
                    continue
                connection, _ = self.sock.accept()
                try:
                    peer(connection)
                    channel = Channel(
                        connection, deadline=self.status["deadline"], on_tick=self.on_tick
                    )
                    channel.send(hello(self.status))
                    if channel.receive() != {
                        "event": "transport-accepted",
                        "nonce": self.status["nonce"],
                    }:
                        raise SandboxUnavailable("Container control acknowledgement mismatch")
                    return channel
                except BaseException:
                    connection.close()
                    raise
            raise SandboxUnavailable("Container control connection deadline expired")
        finally:
            self.close()  # No reconnect or new task in this invocation.

    def close(self):
        self.sock.close()


def relay(sock, incoming, outgoing, *, deadline):
    """Bounded nonblocking bytes; stdin EOF cancels, peer EOF drains output."""
    sock.setblocking(False)
    for descriptor in (incoming, outgoing):
        os.set_blocking(descriptor, False)
    to_peer, to_client = bytearray(), bytearray()
    received = sent = 0
    peer_eof = False
    while True:
        if time.monotonic() >= deadline:
            raise SandboxUnavailable("Container bridge deadline expired")
        if peer_eof and not to_client:
            return
        readers = []
        if not peer_eof and len(to_peer) < BUFFER_BYTES:
            readers.append(incoming)
        if not peer_eof and len(to_client) < BUFFER_BYTES:
            readers.append(sock)
        writers = ([sock] if to_peer and not peer_eof else []) + ([outgoing] if to_client else [])
        readable, writable, _ = select.select(readers, writers, [], 0.05)
        try:
            if incoming in readable:
                chunk = os.read(incoming, BUFFER_BYTES - len(to_peer))
                if not chunk:
                    return  # Closing this socket signals cancellation to the controller.
                received += len(chunk)
                if received > WIRE_BYTES:
                    raise SandboxUnavailable("Container bridge input exceeded its budget")
                to_peer.extend(chunk)
            if sock in readable:
                chunk = sock.recv(BUFFER_BYTES - len(to_client))
                if not chunk:
                    peer_eof = True
                sent += len(chunk)
                if sent > WIRE_BYTES:
                    raise SandboxUnavailable("Container bridge output exceeded its budget")
                to_client.extend(chunk)
            if sock in writable:
                del to_peer[: sock.send(to_peer)]
            if outgoing in writable:
                del to_client[: os.write(outgoing, to_client)]
        except BlockingIOError:
            continue
        except OSError:
            raise SandboxUnavailable("Container bridge disconnected") from None


def bridge():
    status = readiness()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        end = min(time.monotonic() + 3, status["deadline"])
        sock.settimeout(max(0.01, end - time.monotonic()))
        while True:
            try:
                sock.connect(SOCKET)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= end:
                    raise SandboxUnavailable("Private controller did not listen") from None
                time.sleep(0.02)
        peer(sock, child=status["child"])
        relay(sock, 0, 1, deadline=status["deadline"])
