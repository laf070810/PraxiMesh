"""Real local socket mechanics only; no guest or GPU execution proof."""

import os
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from judge.gpu_transport import (
    FRAME_BYTES,
    Channel,
    PrivateListener,
    authorize_peer,
    process_cgroup,
)
from judge.sandbox import SandboxUnavailable


def channels():
    first, second = socket.socketpair()
    return [
        Channel(sock, deadline=time.monotonic() + 3, on_tick=lambda: None)
        for sock in (first, second)
    ]


def test_large_private_request_roundtrip_and_peer_eof():
    sender, receiver = channels()
    request = {
        "source": "transport canary, never executed",
        "inputs": [1.234567890123456e-123] * 8192,
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(receiver.receive)
            sender.send(request)
            assert pending.result(timeout=3) == request
        sender.close()
        receiver.expect_eof()
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]"])
def test_duplicate_and_nonfinite_controller_messages_fail_closed(data):
    sender, receiver = channels()
    try:
        sender.sock.sendall(struct.pack("!I", len(data)) + data)
        with pytest.raises(SandboxUnavailable):
            receiver.receive()
    finally:
        sender.close()
        receiver.close()


def test_excessive_length_rejected_before_waiting_for_or_allocating_payload():
    sender, receiver = channels()
    try:
        sender.sock.sendall(struct.pack("!I", FRAME_BYTES + 1))
        with pytest.raises(SandboxUnavailable, match="budget"):
            receiver.receive()
    finally:
        sender.close()
        receiver.close()


def test_disconnect_and_trailing_output_are_not_success():
    sender, receiver = channels()
    try:
        sender.sock.sendall(b"x")
        with pytest.raises(SandboxUnavailable, match="trailing"):
            receiver.expect_eof()
        sender.close()
        with pytest.raises(SandboxUnavailable, match="disconnected"):
            receiver.connected()
    finally:
        sender.close()
        receiver.close()


def test_callback_wait_cannot_extend_transport_deadline():
    sender, receiver = channels()
    try:
        receiver.deadline = time.monotonic() + 0.01
        receiver.on_tick = lambda: time.sleep(0.03)
        with pytest.raises(SandboxUnavailable, match="deadline"):
            receiver.receive()
    finally:
        sender.close()
        receiver.close()


def test_forced_checkpoint_observes_revoked_ownership_before_sending(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    sender, receiver = channels()
    revoked = False

    def check_owner():
        if revoked:
            raise SandboxUnavailable("original ownership revoked")

    sender.on_tick = check_owner
    try:
        sender.check()
        clock[0] += 0.1
        revoked = True
        sender.check()  # The ordinary periodic check is not due yet.
        with pytest.raises(SandboxUnavailable, match="ownership revoked"):
            sender.check(force=True)
            sender.send({"source": "inert transport fixture"})
        with pytest.raises(BlockingIOError):
            receiver.sock.recv(1)
    finally:
        sender.close()
        receiver.close()


def test_forced_checkpoint_shares_periodic_clock_without_extending_deadline(monkeypatch):
    clock, checks = [10.0], []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    sender, receiver = channels()

    def observe():
        checks.append(clock[0])
        clock[0] += 0.4

    sender.on_tick = observe
    deadline = sender.deadline
    try:
        sender.check(force=True)
        sender.send({"event": "first"})
        assert receiver.receive() == {"event": "first"}
        assert len(checks) == 1
        clock[0] += 0.25
        sender.send({"event": "second"})
        assert receiver.receive() == {"event": "second"}
        assert len(checks) == 2 and sender.deadline == deadline
        clock[0] = deadline - 0.1
        with pytest.raises(SandboxUnavailable, match="deadline"):
            sender.check(force=True)
        assert sender.deadline == deadline
    finally:
        sender.close()
        receiver.close()


def test_reset_with_unread_input_is_a_platform_failure_not_clean_completion():
    sender, receiver = channels()
    try:
        sender.sock.sendall(b"rejected unread input")
        receiver.close()
        with pytest.raises(SandboxUnavailable, match="before clean completion"):
            sender.expect_eof()
    finally:
        sender.close()
        receiver.close()


def test_peer_credentials_are_kernel_observed_and_cgroup_must_match():
    sender, receiver = socket.socketpair()
    try:
        service = SimpleNamespace(
            inspect=lambda: {"ActiveState": "active"}, cgroup_name=process_cgroup(os.getpid())
        )
        assert authorize_peer(receiver, service) == os.getpid()
        service.cgroup_name += "/wrong-task"
        with pytest.raises(SandboxUnavailable, match="does not belong"):
            authorize_peer(receiver, service)
    finally:
        sender.close()
        receiver.close()


def test_listener_contains_only_a_private_socket_and_cleans_up():
    with PrivateListener() as listener:
        directory = listener.path.parent
        assert directory.stat().st_mode & 0o777 == 0o700
        assert listener.path.stat().st_mode & 0o777 == 0o600
        assert list(directory.iterdir()) == [listener.path]
        assert len(str(listener.path).encode()) < 108
    assert not directory.exists()
