"""Actual local pipe mechanics plus fail-closed trusted invocation checks."""

import json
import os
import socket
import struct
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge import container_connection, container_transport
from judge.container_connection import ContainerConnection
from judge.container_runtime_service import RuntimeContainerService
from judge.container_transport import BUFFER_BYTES, ContainerListener, hello, peer, readiness, relay
from judge.gpu_device_lock import DeviceLock
from judge.gpu_transport import Channel
from judge.sandbox import SandboxUnavailable


def test_host_cannot_start_private_listener(monkeypatch):
    monkeypatch.setattr(container_transport.os, "getuid", lambda: 2031)
    with pytest.raises(SandboxUnavailable, match="private trusted"):
        readiness()


@pytest.mark.parametrize("credentials", [(0, 65532, 65532), (2, 0, 65532), (2, 65532, 0)])
def test_peer_requires_private_nonroot_identity(credentials):
    sock = SimpleNamespace(getsockopt=lambda *_a: struct.pack("3i", *credentials))
    with pytest.raises(SandboxUnavailable, match="Unexpected private"):
        peer(sock)


@pytest.mark.parametrize("mismatch", ["pid", "group", "entry"])
def test_wrong_controller_or_unreviewed_bridge_is_rejected(monkeypatch, mismatch):
    sock = SimpleNamespace(getsockopt=lambda *_a: struct.pack("3i", 25, 65532, 65532))
    monkeypatch.setattr(
        Path, "read_text", lambda *_a: "0::/other" if mismatch == "group" else "0::/"
    )
    monkeypatch.setattr(Path, "read_bytes", lambda *_a: b"/bin/unreviewed\0")
    with pytest.raises(SandboxUnavailable, match="owned controller"):
        peer(sock, child=None if mismatch == "entry" else 26 if mismatch == "pid" else 25)


def test_bridge_handles_backpressure_and_drains_peer_eof():
    # Real pipes and sockets; small pipe buffers force partial, nonblocking IO.
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    local, remote = socket.socketpair()
    first, second = b"a" * (BUFFER_BYTES * 9 + 3), b"b" * (BUFFER_BYTES * 7 + 1)

    def read_all(fd, size):
        data = bytearray()
        while len(data) < size:
            chunk = os.read(fd, min(8192, size - len(data)))
            assert chunk
            data.extend(chunk)
        return bytes(data)

    def exchange():
        got = bytearray()
        while len(got) < len(first):
            got.extend(remote.recv(min(8192, len(first) - len(got))))
        assert bytes(got) == first
        remote.sendall(second)
        remote.shutdown(socket.SHUT_WR)

    def send():
        position = 0
        while position < len(first):
            position += os.write(input_write, first[position:])

    try:
        with ThreadPoolExecutor(3) as pool:
            forward = pool.submit(
                relay, local, input_read, output_write, deadline=time.monotonic() + 5
            )
            work = pool.submit(exchange)
            write = pool.submit(send)
            assert read_all(output_read, len(second)) == second
            for future in (forward, work, write):
                future.result(timeout=6)
    finally:
        local.close()
        remote.close()
        for fd in (input_read, input_write, output_read, output_write):
            os.close(fd)


def test_bridge_input_eof_finishes_without_waiting_for_peer():
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    local, remote = socket.socketpair()
    try:
        os.close(input_write)
        relay(local, input_read, output_write, deadline=time.monotonic() + 1)
    finally:
        local.close()
        remote.close()
        for fd in (input_read, output_read, output_write):
            os.close(fd)


@pytest.mark.parametrize("direction", ["input", "output"])
def test_bridge_raw_bytes_are_bounded(monkeypatch, direction):
    monkeypatch.setattr(container_transport, "WIRE_BYTES", 8)
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    local, remote = socket.socketpair()
    try:
        if direction == "input":
            os.write(input_write, b"x" * 9)
        else:
            remote.sendall(b"x" * 9)
        with pytest.raises(SandboxUnavailable, match=direction + " exceeded"):
            relay(local, input_read, output_write, deadline=time.monotonic() + 1)
    finally:
        local.close()
        remote.close()
        for fd in (input_read, input_write, output_read, output_write):
            os.close(fd)


def test_bridge_has_absolute_deadline():
    first, second = socket.socketpair()
    try:
        with pytest.raises(SandboxUnavailable, match="deadline"):
            relay(first, second.fileno(), second.fileno(), deadline=time.monotonic() - 1)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("accepted", [True, False])
def test_listener_requires_original_ack_and_never_reconnects(monkeypatch, tmp_path, accepted):
    status = {"child": os.getpid(), "nonce": "a" * 32, "deadline": time.monotonic() + 3}
    path = str(tmp_path / "control.sock")
    monkeypatch.setattr(container_transport, "SOCKET", path)
    monkeypatch.setattr(container_transport, "readiness", lambda: status)
    exit_read, exit_write = os.pipe()
    monkeypatch.setattr(container_transport, "bridge_process", lambda _sock: os.dup(exit_read))
    listener = ContainerListener(on_tick=lambda: None)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            channel = Channel(sock, deadline=status["deadline"], on_tick=lambda: None)
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(listener.accept)
                assert channel.receive() == hello(status)
                channel.send(
                    {
                        "event": "transport-accepted",
                        "nonce": status["nonce"] if accepted else "wrong",
                    }
                )
                os.write(exit_write, b"x")  # Handshake fixture's process-exit observation.
                if accepted:
                    result.result(timeout=4).close()
                else:
                    with pytest.raises(SandboxUnavailable, match="acknowledgement"):
                        result.result(timeout=4)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as another:
                with pytest.raises(ConnectionRefusedError):
                    another.connect(path)
    finally:
        os.close(exit_read)
        os.close(exit_write)


def test_non_main_process_cannot_listen(monkeypatch):
    monkeypatch.setattr(container_transport, "readiness", lambda: {"child": os.getpid() + 1})
    with pytest.raises(SandboxUnavailable, match="main controller"):
        ContainerListener(on_tick=lambda: None)


@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = SimpleNamespace(path=tmp_path / "engine.sock", check_socket=lambda: None)
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        service = RuntimeContainerService(
            guard, uuid.uuid4(), uuid.uuid4(), engine=engine, image="sha256:" + "a" * 64
        )
        status = {"nonce": "b" * 32, "child": 2, "deadline": time.monotonic() + 10}
        state = {
            "id": "c" * 64,
            "running": True,
            "arguments": ["0", str(status["deadline"]), status["nonce"]],
        }
        monkeypatch.setattr(service, "inspect", lambda: state)
        yield service, status, state


@pytest.mark.parametrize("change", ["nonce", "deadline", "stopped"])
def test_stale_startup_never_invokes_docker(service, monkeypatch, change):
    item, status, state = service
    if change == "stopped":
        state["running"] = False
    else:
        status[change] = "wrong" if change == "nonce" else time.monotonic() - 1
    monkeypatch.setattr(
        container_connection.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Must not start CLI"),
    )
    with pytest.raises(SandboxUnavailable, match="original live"):
        ContainerConnection(item, status, on_tick=lambda: None)


def test_connection_uses_fixed_command_minimal_environment_and_closes_on_bad_identity(
    service, monkeypatch
):
    item, status, state = service
    captures = {}
    actual_popen = subprocess.Popen

    def popen(command, **kwargs):
        captures.update(command=command, config=Path(command[2]), env=kwargs["env"])
        wrong = hello({**status, "child": 999})
        data = json.dumps(wrong).encode()
        program = f"import os; os.write(1, {struct.pack('!I', len(data)) + data!r})"
        process = actual_popen([os.sys.executable, "-c", program], **kwargs)
        captures["process"] = process
        return process

    monkeypatch.setattr(container_connection.subprocess, "Popen", popen)
    monkeypatch.setenv("HTTP_PROXY", "private")
    monkeypatch.setenv("DATABASE_URL", "private")
    with pytest.raises(SandboxUnavailable, match="startup identity"):
        ContainerConnection(item, status, on_tick=lambda: None)
    assert set(captures["env"]) == {"PATH", "LC_ALL"}
    assert captures["command"][8:] == [
        state["id"],
        "/usr/local/bin/python",
        "-I",
        container_transport.BRIDGE,
    ]
    assert captures["process"].poll() is not None and not captures["config"].exists()
