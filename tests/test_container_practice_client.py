"""Real framed subprocesses and private state; Docker/GPU remain explicit models."""

import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from judge import container_practice_client as module
from judge.container_practice_host import RECORD
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_practice_controller import IMAGE, STARTED
from tests.test_container_practice_controller import lifecycle as lifecycle
from tests.test_container_practice_host import prepared as prepared
from tests.test_gpu_public_execution import public_problem as public_problem

ROOT = Path(__file__).resolve().parents[1]
CHILD = r"""
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from judge.container_journal import ContainerJournal
from judge.container_practice_state import LocalPracticeState, OPERATION
from judge.container_practice_transport import PipeChannel
from judge.gpu_device_lock import DeviceLock
from judge.gpu_receipt import CHECKS
from judge.container_practice_protocol import PUBLIC_SCOPE
from judge.results import LABELS
root, case = Path(sys.argv[2]), sys.argv[3]
record = json.loads((root / 'journal/local-controller.json').read_text())
identity = json.loads((root / 'journal/installation.json').read_text())
class Engine:
    def verify(self): pass
    def run(self, *args, **kwargs):
        assert args[0] == 'info'
        return SimpleNamespace(returncode=0, stdout=identity['engine'].encode())
with DeviceLock(root / 'locks', record['hello']['device']) as guard:
  with ContainerJournal(root / 'journal', guard, Engine()) as journal:
    with LocalPracticeState(journal) as state:
      with PipeChannel(0, 1, deadline=record['hello']['deadline'], on_tick=state.check) as channel:
        if case == 'stderr-overflow':
            sys.stderr.buffer.write(b'x' * 70000); sys.stderr.buffer.flush()
        greeting = dict(record['hello'])
        if case == 'wrong-hello': greeting['nonce'] = '0' * 32
        channel.send(greeting)
        request = channel.receive()
        if case == 'hold': time.sleep(15)
        if record['hello']['mode'] == 'run':
            assert request['event'] == 'prepare-local'
            env = json.loads((root / 'journal/fixture-environment.json').read_text())
            sent = dict(env)
            if case == 'wrong-environment': sent['schema'] = -1
            channel.send({'event': 'local-prepared', 'nonce': record['nonce'], 'environment': sent})
            request = channel.receive()
            assert request['event'] == 'execute-public'
            journal.write('fixture-source-received.json', {'received': True})
            operation = state.begin(env)
            for index, name in enumerate(CHECKS, 1):
                channel.send({'event': 'local-progress', 'nonce': record['nonce'], 'index': index, 'name': name})
            if case != 'unfinished-operation': state.finish(operation)
            names = [item['name'] for item in request['problem']['tests']]
            completion = {'verdict': 'accepted', 'label': LABELS['accepted'],
                'passed': len(names), 'total': len(names), 'compile_initialization_ms': 1.0,
                'public_cases': [{'name': name, 'verdict': 'accepted', 'label': LABELS['accepted']} for name in names]}
            result = {'scope': PUBLIC_SCOPE, 'formal_result': False, 'ready': False,
                'environment': env, 'startup_checks': len(CHECKS),
                'startup_evidence_digest': 'a' * 64, 'result': completion}
        else:
            assert request == {'event': record['hello']['mode']}
            status = 'initialized' if request['event'] == 'initialize' else state.recover()['status']
            result = {'status': status, 'ready': False, 'formal_result': False}
        if case == 'wrong-state': journal.write(OPERATION, {'unrelated': True})
        channel.send({'event': 'local-completed', 'nonce': record['nonce'], 'result': result})
        if case == 'extra-output': sys.stdout.buffer.write(b'x'); sys.stdout.buffer.flush()
        if case == 'exit-error': sys.exit(7)
"""


@pytest.fixture
def framed(lifecycle, monkeypatch):
    value = lifecycle
    value.process, value.case, value.spawned = None, "normal", 0
    original_popen, original_run = subprocess.Popen, value.run

    def spawn(command, **kwargs):
        value.spawned += 1
        assert command[:2] == ["docker", "--config"]
        assert command[3:] == [
            "--host",
            "unix://" + str(value.host.engine.path),
            "container",
            "start",
            "--attach",
            "--interactive",
            value.host.record["id"],
        ]
        assert not Path(command[2]).exists()
        assert set(kwargs["env"]) == {"PATH", "LC_ALL"} and kwargs["bufsize"] == 0
        process = original_popen(
            [sys.executable, "-I", "-c", CHILD, str(ROOT), str(value.host.root), value.case],
            **kwargs,
        )
        value.process = process
        value.state["State"].update(
            Running=True, Status="running", Pid=process.pid, StartedAt=STARTED
        )
        return process

    def run(*args, **kwargs):
        if args[:2] == ("container", "create"):
            value.process = None
        if value.process is not None and value.state is not None:
            code = value.process.poll()
            if code is not None:
                value.state["State"].update(
                    Running=False,
                    Status="exited",
                    Pid=0,
                    ExitCode=code if code >= 0 else 128 - code,
                    FinishedAt="2026-11-02T01:00:02Z",
                )
            if args[:2] == ("container", "kill") and code is None:
                value.process.kill()
                value.process.wait(timeout=5)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(value.host.engine, "run", run)
    try:
        yield value
    finally:
        if value.process is not None:
            if value.process.poll() is None:
                value.process.kill()
            value.process.wait(timeout=5)


def test_result_waits_for_original_process_container_and_terminal_record(framed):
    value = framed
    result = module.invoke(value.host, IMAGE, "initialize")
    assert result == {"status": "initialized", "ready": False, "formal_result": False}
    assert value.process.returncode == 0 and value.state is None
    assert value.host.record["phase"] == "completed"
    assert not list((value.host.root / "journal").glob("local-docker-config-*"))
    assert value.spawned == 1


@pytest.mark.parametrize("case", ["normal", "wrong-environment", "unfinished-operation"])
def test_public_run_and_durable_recovery_with_real_pipes(
    framed, configured, public_problem, monkeypatch, case
):
    value, adapter = framed, configured.adapter
    value.case = case
    environment = adapter.environment
    path = value.host.root / "journal/fixture-environment.json"
    path.write_text(json.dumps(environment))
    path.chmod(0o600)
    monkeypatch.setattr(module, "expected_environment", lambda *_args: environment)
    progress = []

    def run():
        return module.invoke(
            value.host,
            IMAGE,
            "run",
            image=adapter.image,
            driver=adapter.driver_manifest,
            problem=public_problem,
            source="opaque source",
            on_progress=progress.append,
        )

    if case == "normal":
        result = run()
        assert result["formal_result"] is False and result["ready"] is False
        assert result["result"]["verdict"] == "accepted"
        assert len(progress) == result["startup_checks"] == 24
        with value.host.physical() as state:
            assert state.operation()["phase"] == "completed"
    else:
        with pytest.raises(SandboxUnavailable):
            run()
        if case == "wrong-environment":
            assert not (value.host.root / "journal/fixture-source-received.json").exists()
        value.case = "normal"
        result = module.invoke(value.host, IMAGE, "recover")
        assert result["status"] == ("recovered" if case == "unfinished-operation" else "idle")
    assert value.state is None and value.host.record["phase"] == "completed"


@pytest.mark.parametrize(
    "case", ["wrong-hello", "wrong-state", "extra-output", "exit-error", "stderr-overflow"]
)
def test_partial_protocol_or_failed_exit_never_returns_success(framed, case):
    value = framed
    value.case = case
    with pytest.raises(SandboxUnavailable):
        module.invoke(value.host, IMAGE, "initialize")
    assert value.host.record["phase"] == "removed"
    assert value.state is None and value.process.poll() is not None
    assert not list((value.host.root / "journal").glob("local-docker-config-*"))


def test_deadline_still_cleans_original_outer_container(framed, monkeypatch):
    value = framed
    value.case = "hold"
    monkeypatch.setattr(module, "MAINTENANCE_SECONDS", 1.5)
    with pytest.raises((SandboxUnavailable, ValueError)):
        module.invoke(value.host, IMAGE, "initialize")
    assert value.host.record["phase"] == "removed" and value.state is None
    assert value.process.poll() is not None


def test_cancel_after_completed_frame_withholds_result_and_preserves_terminal_record(framed):
    value = framed

    def cancel():
        record = value.host.read(RECORD)
        if record is not None and record["phase"] == "completed":
            raise InterruptedError("fixed final cancellation")

    with pytest.raises(InterruptedError, match="final cancellation"):
        module.invoke(value.host, IMAGE, "initialize", on_tick=cancel)
    assert value.host.record["phase"] == "completed" and value.state is None


def test_cancellation_during_attachment_stops_only_original(framed):
    value = framed
    value.case = "hold"

    def cancel():
        record = value.host.read(RECORD)
        if record is not None and record["phase"] == "running":
            raise InterruptedError("fixed active cancellation")

    with pytest.raises(InterruptedError, match="active cancellation"):
        module.invoke(value.host, IMAGE, "initialize", on_tick=cancel)
    assert value.host.record["phase"] == "removed" and value.state is None
    assert value.process.poll() is not None


def test_pending_outer_operation_requires_explicit_recovery(framed):
    value = framed
    value.case = "exit-error"
    with pytest.raises(SandboxUnavailable):
        module.invoke(value.host, IMAGE, "initialize")
    with pytest.raises(SandboxUnavailable, match="Recover the original"):
        module.invoke(value.host, IMAGE, "initialize")
    assert value.spawned == 1
    value.case = "normal"
    result = module.invoke(value.host, IMAGE, "recover")
    assert result["status"] == "idle" and value.host.record["phase"] == "completed"
    assert value.spawned == 2


def test_existing_docker_configuration_is_not_read_or_deleted(lifecycle):
    value = lifecycle

    value.control.prepare(IMAGE, "initialize", time.monotonic() + 60)
    value.control.create()
    attached = module.AttachedController(value.control, lambda: None)
    attached.config.mkdir(mode=0o700)
    with pytest.raises(SandboxUnavailable, match="Unexpected local Docker"):
        with attached:
            pytest.fail("Existing configuration must not be used")
    assert attached.config.is_dir() and value.host.record["phase"] == "created"
    value.control.remove()


def test_public_terminal_state_must_be_new_and_match_expected_environment():
    before = {
        "job": "old",
        "reservation": "old-reservation",
        "phase": "completed",
        "environment": {"version": 1},
    }
    after = {**before, "job": "new", "reservation": "new-reservation"}
    module.completed_operation("run", before, after, {}, {"version": 1})
    for changed in (
        before,
        {**after, "phase": "pending"},
        {**after, "environment": {"version": True}},
        None,
    ):
        with pytest.raises(SandboxUnavailable):
            module.completed_operation("run", before, changed, {}, {"version": 1})


def test_recovery_preserves_original_job_reservation_and_environment():
    before = {
        "job": "original",
        "reservation": "original-reservation",
        "phase": "pending",
        "environment": {"version": 1},
    }
    after = {**before, "phase": "recovered"}
    module.completed_operation("recover", before, after, {"status": "recovered"}, None)
    for changed in (
        {**after, "job": "replacement"},
        {**after, "reservation": "replacement"},
        {**after, "environment": {"version": True}},
        before,
    ):
        with pytest.raises(SandboxUnavailable):
            module.completed_operation("recover", before, changed, {"status": "recovered"}, None)


def test_shared_environment_matches_container_adapter_without_mutation(configured):
    adapter = configured.adapter
    original = copy.deepcopy(adapter.environment)
    actual = module.configured_environment(
        original["target"],
        original["device_spec"],
        image=adapter.image,
        driver_manifest=adapter.driver_manifest,
        management_manifest=original["artifacts"]["management_manifest"],
        engine=configured.engine,
    )
    assert actual == original and adapter.environment == original
    actual["target"]["model"] = "changed copy"
    assert adapter.environment == original


def test_host_expected_environment_uses_independent_kernel_and_original_metadata(
    lifecycle, configured, monkeypatch
):
    host = lifecycle.host
    monkeypatch.setattr(
        host.engine, "verify", lambda: {**configured.engine, "ID": host.installation["engine"]}
    )
    monkeypatch.setattr(
        module,
        "read_file",
        lambda fd, name, maximum: "Character devices:\n509 nvidia-uvm\nBlock devices:\n",
    )
    actual = module.expected_environment(
        host, configured.adapter.image, configured.adapter.driver_manifest
    )
    assert actual == configured.adapter.environment


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_deferred_signal_is_observed_synchronously_and_handler_restored(number):
    original = signal.getsignal(number)
    with module.deferred_signals() as check:
        os.kill(os.getpid(), number)
        with pytest.raises(InterruptedError):
            check()
    assert signal.getsignal(number) == original


def test_client_import_has_no_application_or_database_dependency():
    script = "import sys; sys.path.insert(0,sys.argv[1]); import judge.container_practice_client; assert not any(n == 'django' or n.startswith('django.') or n == 'core' or n.startswith('core.') for n in sys.modules)"
    subprocess.run([sys.executable, "-I", "-c", script, str(ROOT)], check=True, timeout=20)
