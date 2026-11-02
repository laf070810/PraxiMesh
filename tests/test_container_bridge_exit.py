"""A controller must outlive its original bridge, even after socket EOF."""

import ctypes
import os
import select
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from judge import container_transport
from judge.container_transport import ContainerListener, bridge_process
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def pidfds(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Container process handles require Linux")
    opened = []
    native = getattr(os, "pidfd_open", None)

    def open_pidfd(pid):
        if native is not None:
            descriptor = native(pid)
        else:
            # Portable uv Python omits the wrapper on this Linux test host.
            # The fixed container Python has it; exercise the actual kernel here.
            assert os.uname().machine in {"x86_64", "aarch64"}
            libc = ctypes.CDLL(None, use_errno=True)
            descriptor = libc.syscall(434, pid, 0)
            if descriptor < 0:
                raise OSError(ctypes.get_errno(), "pidfd_open failed")
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "pidfd_open", open_pidfd, raising=False)
    yield opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def line(process):
    assert select.select([process.stdout], [], [], 3)[0], "Peer did not reach its barrier"
    return process.stdout.readline()


@pytest.fixture
def live_peer(tmp_path, monkeypatch, pidfds):
    path = str(tmp_path / "control.sock")
    status = {"child": os.getpid(), "nonce": "a" * 32, "deadline": time.monotonic() + 8}
    monkeypatch.setattr(container_transport, "SOCKET", path)
    monkeypatch.setattr(container_transport, "readiness", lambda: status)
    listener = ContainerListener(on_tick=lambda: None)
    program = """
import socket, sys, time
from judge.gpu_transport import Channel
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(sys.argv[1])
channel = Channel(sock, deadline=time.monotonic() + 10, on_tick=lambda: None)
status = channel.receive()
channel.send({'event': 'transport-accepted', 'nonce': status['nonce']})
print('connected', flush=True)
channel.expect_eof()
print('eof', flush=True)
sys.stdin.buffer.read(1)
channel.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Only private-container identity is substituted; process lifetime and IO are real.
    monkeypatch.setattr(container_transport, "peer", lambda _sock: process.pid)
    channel = None
    try:
        channel = listener.accept()
        assert line(process) == b"connected\n"
        yield channel, process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
        if channel is not None:
            channel.close()
        listener.close()


def test_socket_close_waits_for_original_bridge_exit(live_peer):
    channel, process = live_peer
    with ThreadPoolExecutor(1) as pool:
        closing = pool.submit(channel.close)
        try:
            assert line(process) == b"eof\n"
            assert process.poll() is None
            assert not closing.done(), "Controller returned while its bridge was still alive"
        finally:
            process.stdin.write(b"x")
            process.stdin.flush()
        closing.result(timeout=3)
    assert process.wait(timeout=3) == 0
    channel.close()  # A second close must not observe or close a reused descriptor.


def test_bridge_exit_wait_keeps_original_deadline(live_peer):
    channel, process = live_peer
    channel.deadline = time.monotonic() + 0.15
    before = time.monotonic()
    with pytest.raises(SandboxUnavailable, match="bridge.*exit"):
        channel.close()
    assert time.monotonic() - before < 1
    assert process.poll() is None  # The observer never kills a process.
    assert channel.sock.fileno() == -1


def test_bridge_exit_wait_honors_cancellation_and_closes_handle(live_peer):
    channel, process = live_peer

    def cancelled():
        raise SandboxUnavailable("Original invocation cancelled")

    channel.on_tick = cancelled
    with pytest.raises(SandboxUnavailable, match="cancelled"):
        channel.close()
    assert process.poll() is None
    assert channel.sock.fileno() == -1


@pytest.mark.parametrize("unavailable", ["missing", "denied"])
def test_unobservable_bridge_is_refused(monkeypatch, unavailable):
    monkeypatch.setattr(container_transport, "peer", lambda _sock: os.getpid())
    if unavailable == "missing":
        monkeypatch.delattr(os, "pidfd_open", raising=False)
    else:

        def denied(_pid):
            raise PermissionError("Denied by the kernel")

        monkeypatch.setattr(os, "pidfd_open", denied, raising=False)
    with pytest.raises(SandboxUnavailable, match="could not be observed"):
        bridge_process(object())


@pytest.mark.parametrize("change", ["identity", "exit"])
def test_changed_peer_is_refused_and_process_handle_closed(monkeypatch, pidfds, change):
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    calls = 0

    def observe(_sock):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "identity":
                return process.pid + 1
            process.stdin.close()
            process.wait(timeout=3)
        return process.pid

    monkeypatch.setattr(container_transport, "peer", observe)
    try:
        with pytest.raises(SandboxUnavailable, match="changed before acceptance"):
            bridge_process(object())
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdin.close()
