"""Real framed pipes/private state; GPU and daemon methods are explicit models."""

import copy
import os
import struct
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from judge import container_practice_protocol as protocol
from judge.container_practice_state import LocalPracticeState
from judge.container_practice_transport import PipeChannel
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_receipt import CHECKS
from judge.gpu_task_protocol import public_task_request
from judge.gpu_transport import FRAME_BYTES
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_practice import local as local
from tests.test_gpu_public_execution import public_completion
from tests.test_gpu_public_execution import public_problem as public_problem


class Frames:
    def __init__(self, incoming=(), *, deadline=None):
        self.incoming = list(incoming)
        self.sent = []
        self.deadline = deadline or time.monotonic() + 120
        self.finished = False
        self.on_tick = lambda: None

    def receive(self):
        return copy.deepcopy(self.incoming.pop(0))

    def send(self, value):
        self.sent.append(copy.deepcopy(value))

    def connected(self):
        pass

    def expect_eof(self):
        assert not self.incoming
        self.finished = True


@pytest.fixture
def conversation(local, public_problem):
    channel = Frames()
    with LocalPracticeState(local.journal, create=True) as state:
        nonce = uuid.uuid4().hex
        hello = protocol.hello(
            "run",
            local.guard.device_uuid,
            nonce,
            channel.deadline,
            canonical_digest(state.marker["binding"]),
        )
        environment = copy.deepcopy(local.adapter.environment)
        request = public_task_request(public_problem, "opaque source")
        result = {
            "scope": protocol.PUBLIC_SCOPE,
            "formal_result": False,
            "ready": False,
            "startup_checks": len(CHECKS),
            "startup_evidence_digest": "a" * 64,
            "environment": environment,
            "result": public_completion(request)["result"],
        }
        channel.incoming = [
            hello,
            {"event": "local-prepared", "nonce": nonce, "environment": environment},
        ]
        channel.incoming += [
            {"event": "local-progress", "nonce": nonce, "index": i, "name": name}
            for i, name in enumerate(CHECKS, 1)
        ]
        channel.incoming.append({"event": "local-completed", "nonce": nonce, "result": result})
        yield (
            channel,
            hello,
            {
                "image": local.adapter.image,
                "driver": local.adapter.driver_manifest,
                "environment": environment,
                "problem": public_problem,
                "source": "opaque source",
            },
            state,
        )


def test_client_verifies_all_stages_completion_and_eof(conversation):
    channel, hello, options, _state = conversation
    progress = []
    result = protocol.exchange(channel, hello, **options, on_progress=progress.append)
    assert progress == list(CHECKS) and channel.finished
    assert [value["event"] for value in channel.sent] == ["prepare-local", "execute-public"]
    assert result["ready"] is False and result["formal_result"] is False


@pytest.mark.parametrize("point", ["prepared", "eof"])
def test_client_checks_late_cancellation_before_source_and_return(conversation, point):
    channel, hello, options, _state = conversation
    receive = channel.receive
    eof = channel.expect_eof
    cancelled = False

    def read():
        nonlocal cancelled
        value = receive()
        if point == "prepared" and value["event"] == "local-prepared":
            cancelled = True
        return value

    def finish():
        nonlocal cancelled
        eof()
        cancelled = True

    def check():
        if cancelled:
            raise InterruptedError("Fixed late cancellation")

    channel.receive, channel.expect_eof, channel.on_tick = read, finish, check
    with pytest.raises(InterruptedError):
        protocol.exchange(channel, hello, **options)
    if point == "prepared":
        assert all(value["event"] != "execute-public" for value in channel.sent)


@pytest.mark.parametrize("phase", ["handshake", "preparation"])
@pytest.mark.parametrize("change", ["nonce", "extra", "environment"])
def test_client_withholds_source_until_both_identities_match(conversation, phase, change):
    channel, hello, options, _state = conversation
    message = channel.incoming[0 if phase == "handshake" else 1] = copy.deepcopy(
        channel.incoming[0 if phase == "handshake" else 1]
    )
    message[change] = "wrong-original-identity"
    with pytest.raises(SandboxUnavailable):
        protocol.exchange(channel, hello, **options)
    assert all(value["event"] != "execute-public" for value in channel.sent)


@pytest.mark.parametrize("change", ["boolean", "order", "nonce", "missing", "premature"])
def test_client_rejects_incomplete_or_retyped_progress(conversation, change):
    channel, hello, options, _state = conversation
    if change == "boolean":
        channel.incoming[2]["index"] = True
    elif change == "order":
        channel.incoming[2], channel.incoming[3] = channel.incoming[3], channel.incoming[2]
    elif change == "nonce":
        channel.incoming[2]["nonce"] = uuid.uuid4().hex
    elif change == "missing":
        del channel.incoming[2]
    else:
        channel.incoming[2] = channel.incoming[-1]
    with pytest.raises(SandboxUnavailable):
        protocol.exchange(channel, hello, **options)
    assert not channel.finished


@pytest.mark.parametrize(
    "change", ["formal", "ready", "performance", "count", "environment", "digest", "extra"]
)
def test_client_rejects_changed_or_scored_completion(conversation, change):
    channel, hello, options, _state = conversation
    result = channel.incoming[-1]["result"] = copy.deepcopy(channel.incoming[-1]["result"])
    if change == "formal":
        result["formal_result"] = 0
    elif change == "ready":
        result["ready"] = True
    elif change == "performance":
        result["result"]["performance"] = {"score": 1}
    elif change == "count":
        result["startup_checks"] -= 1
    elif change == "environment":
        result["environment"]["target"]["index"] = False
    elif change == "digest":
        result["startup_evidence_digest"] = "invalid"
    else:
        result["receipt"] = "not-a-receipt"
    with pytest.raises(SandboxUnavailable):
        protocol.exchange(channel, hello, **options)
    assert not channel.finished


@contextmanager
def pipes(deadline):
    client_in, server_out = os.pipe()
    server_in, client_out = os.pipe()
    try:
        with PipeChannel(client_in, client_out, deadline=deadline, on_tick=lambda: None) as client:
            with PipeChannel(
                server_in, server_out, deadline=deadline, on_tick=lambda: None
            ) as server:
                for descriptor in (client_in, server_out, server_in, client_out):
                    os.close(descriptor)
                yield client, server
    finally:
        # The original descriptors were transferred before the yield.
        pass


def test_real_pipes_connect_full_local_lifecycle_and_original_recovery(local, public_problem):
    with LocalPracticeState(local.journal, create=True) as state:
        for mode in ("run", "recover"):
            errors = []
            with pipes(time.monotonic() + 120) as (client, server):
                nonce = uuid.uuid4().hex
                expected = protocol.hello(
                    mode,
                    local.guard.device_uuid,
                    nonce,
                    client.deadline,
                    canonical_digest(state.marker["binding"]),
                )
                original_send = server.send

                def send(value, original_send=original_send):
                    if value["event"] == "local-progress":
                        local.progress.append(value["name"])
                    original_send(value)

                server.send = send

                def serve(mode=mode, nonce=nonce, errors=errors, server=server):
                    try:
                        protocol.serve(
                            local.control,
                            state,
                            server,
                            mode=mode,
                            nonce=nonce,
                            on_tick=lambda: None,
                        )
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        server.close()

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                options = (
                    {}
                    if mode == "recover"
                    else {
                        "image": local.adapter.image,
                        "driver": local.adapter.driver_manifest,
                        "environment": local.adapter.environment,
                        "problem": public_problem,
                        "source": "opaque source",
                    }
                )
                try:
                    result = protocol.exchange(client, expected, **options)
                finally:
                    client.close()
                    thread.join(timeout=10)
                assert not thread.is_alive() and not errors, errors
                assert result["formal_result"] is False and result["ready"] is False
                assert state.operation()["phase"] == "completed" and local.journal.load() is None
        assert local.public_calls == 1 and local.progress == list(CHECKS)


@pytest.mark.parametrize("mode", ["initialize", "recover"])
def test_maintenance_never_builds_an_execution_adapter(local, monkeypatch, mode):
    def forbidden(*args, **kwargs):
        raise AssertionError("Maintenance constructed an execution adapter")

    monkeypatch.setattr(protocol, "ContainerGPUAdapter", forbidden)
    with LocalPracticeState(local.journal, create=True) as state:
        if mode == "recover":
            state.begin(local.adapter.environment)
        channel = Frames([{"event": mode}])
        result = protocol.serve(
            local.control, state, channel, mode=mode, nonce=uuid.uuid4().hex, on_tick=lambda: None
        )
        assert result["status"] == ("initialized" if mode == "initialize" else "recovered")
        assert local.journal.load() is None and local.public_calls == 0


def test_pending_operation_refuses_preparation_without_accessing_new_adapter(local, monkeypatch):
    with LocalPracticeState(local.journal, create=True) as state:
        original = state.begin(local.adapter.environment)
        channel = Frames(
            [protocol.configuration(local.adapter.image, local.adapter.driver_manifest)]
        )
        monkeypatch.setattr(
            protocol, "ContainerGPUAdapter", lambda *args, **kwargs: pytest.fail("New adapter")
        )
        with pytest.raises(SandboxUnavailable, match="Recover the original"):
            protocol.serve(
                local.control,
                state,
                channel,
                mode="run",
                nonce=uuid.uuid4().hex,
                on_tick=lambda: None,
            )
        assert state.operation() == original and len(channel.sent) == 1


@pytest.mark.parametrize("action", ["disconnect", "unexpected", "cancel"])
def test_pipe_loss_cancels_full_startup_and_preserves_original_operation(
    local, public_problem, action
):
    with LocalPracticeState(local.journal, create=True) as state:
        errors = []
        cancelled = False
        with pipes(time.monotonic() + 120) as (client, server):
            nonce = uuid.uuid4().hex

            def check():
                if cancelled:
                    raise InterruptedError("Fixed cancellation")

            def serve():
                try:
                    protocol.serve(
                        local.control, state, server, mode="run", nonce=nonce, on_tick=check
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    server.close()

            original_send = server.send

            def send(value):
                nonlocal cancelled
                original_send(value)
                if value["event"] == "local-progress":
                    local.progress.append(value["name"])
                    if action == "cancel":
                        cancelled = True
                    elif action == "unexpected":
                        client.send({"event": "unsolicited"})
                    else:
                        client.close()

            server.send = send
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client.receive()
            client.send(protocol.configuration(local.adapter.image, local.adapter.driver_manifest))
            assert client.receive()["event"] == "local-prepared"
            client.send(public_task_request(public_problem, "opaque source"))
            thread.join(timeout=15)
            assert not thread.is_alive() and len(errors) == 1
            assert isinstance(errors[0], (SandboxUnavailable, InterruptedError))
        assert state.operation()["phase"] == "pending"
        assert local.public_calls == 0 and local.journal.load() is None
        assert state.recover()["status"] == "recovered"


@pytest.mark.parametrize(
    "payload", [b"", b"\x00\x00", struct.pack("!I", FRAME_BYTES + 1), struct.pack("!I", 4) + b"{"]
)
def test_pipe_eof_and_oversized_frames_fail_closed(payload):
    with pipes(time.monotonic() + 2) as (client, server):
        if payload:
            os.write(server.outgoing, payload)
        server.close()
        with pytest.raises(SandboxUnavailable):
            client.receive()


def test_pipe_backpressure_has_a_deadline_and_keeps_cancellation_responsive():
    with pipes(time.monotonic() + 0.15) as (client, _server):
        with pytest.raises(SandboxUnavailable, match="deadline"):
            client.send({"data": "x" * (1024 * 1024)})


def test_real_subprocess_uses_framed_stdio_without_django():
    root = Path(__file__).resolve().parents[1]
    program = f"""
import sys,time
sys.path.insert(0,{str(root)!r})
from judge.container_practice_transport import PipeChannel
from judge import container_practice_protocol
assert not any(name == 'django' or name.startswith('django.') or name == 'core' for name in sys.modules)
with PipeChannel(0,1,deadline=time.monotonic()+5,on_tick=lambda:None) as channel:
    value=channel.receive()
    channel.send({{'echo':value}})
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        with PipeChannel(
            process.stdout.fileno(),
            process.stdin.fileno(),
            deadline=time.monotonic() + 5,
            on_tick=lambda: None,
        ) as channel:
            channel.send({"public": "opaque, never evaluated source"})
            assert channel.receive() == {"echo": {"public": "opaque, never evaluated source"}}
            channel.expect_eof()
        assert process.wait(timeout=3) == 0, process.stderr.read().decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize(
    "field,value", [(0, "submit"), (1, "0"), (2, "bad"), (3, float("inf")), (3, True), (3, -1)]
)
def test_entry_invocation_rejects_unbounded_or_ambiguous_input(field, value):
    args = ["run", "GPU-" + str(uuid.uuid4()), uuid.uuid4().hex, time.monotonic() + 10]
    args[field] = value
    with pytest.raises(ValueError):
        protocol.invocation(*args)
