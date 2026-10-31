"""Real database/host lock lifecycle with explicit GPU runtime doubles only."""

import copy
import signal
import subprocess
import sys
import uuid
from selectors import EVENT_READ, DefaultSelector
from threading import Event
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core import gpu_readiness, gpu_worker
from core.gpu_resources import DeviceOwnershipError
from core.gpu_worker import StopFlag, run_worker, stop_signals
from core.models import GPUDevice, Submission, WorkerHealth
from judge import gpu_receipt
from judge.container_journal import ContainerJournal
from judge.gpu_device_lock import DeviceLock
from judge.gpu_kernel_checks import check_request
from judge.gpu_receipt import CHECKS, GPUReceiptExpired, ReceiptStore, binding
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import Engine, intent
from tests.test_gpu_isolation import REQUEST, resource_report
from tests.test_gpu_kernel_checks import check_report
from tests.test_gpu_receipt import adapter_for
from tests.test_gpu_resources import gpu_version as gpu_version
from tests.test_gpu_resources import queued

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def worker(tmp_path, monkeypatch, user, gpu_version):
    adapter = adapter_for(tmp_path)
    with DeviceLock(tmp_path / "locks", adapter.target["uuid"]) as guard:
        state = SimpleNamespace(
            guard=guard,
            adapter=adapter,
            worker_id="worker-unit-test",
            stop=Event(),
            directory=tmp_path / "receipts",
            events=[],
            phase_hook=None,
            execute_hook=None,
        )
        state.path = state.directory / (guard.device_uuid + ".json")
        monkeypatch.setattr(OwnedJobService, "inspect", lambda _self: None)
        monkeypatch.setattr(OwnedJobService, "populated", lambda _self: False)

        def stopped(service):
            state.events.append(("stop", service.purpose, service.job_id))
            return {"unit": service.unit, "stopped": True, "cgroup_empty": True}

        monkeypatch.setattr(OwnedJobService, "stop", stopped)
        monkeypatch.setattr(gpu_readiness, "identify", lambda _uuid: copy.deepcopy(adapter.target))
        monkeypatch.setattr(gpu_readiness.DeviceMonitor, "observe", lambda _self: [])

        def phase(name, **kwargs):
            kwargs["on_tick"]()
            device = GPUDevice.objects.get(pk=guard.device_uuid)
            assert device.state == "quarantined" and device.checked_at is None
            state.events.append(name)
            if state.phase_hook:
                state.phase_hook(name)
            kwargs["on_tick"]()

        def recover(_service, **kwargs):
            phase("initial-recovery", **kwargs)
            return copy.deepcopy(adapter.environment)

        def resources(_service, **kwargs):
            phase("resources", **kwargs)
            return resource_report(REQUEST, adapter.target), copy.deepcopy(adapter.environment)

        def kernel(_service, name, **kwargs):
            phase("kernel/" + name, **kwargs)
            return check_report(check_request(name, uuid.uuid4().hex)), copy.deepcopy(
                adapter.environment
            )

        def controller(_service, name, **kwargs):
            phase("controller/" + name, **kwargs)
            return {
                "name": name,
                "challenge": uuid.uuid4().hex,
                "disconnect_seconds": 20.0,
                "independent_stop": name != "transport-loss",
                "recovered": True,
            }, copy.deepcopy(adapter.environment)

        def evaluate(service, **kwargs):
            state.events.append("execute")
            kwargs["on_begin"]()
            if state.execute_hook:
                state.execute_hook(service, kwargs)
            kwargs["on_tick"]()
            state.events.append("adapter-physical-recovery-double")
            return {"verdict": "wrong_answer"}, copy.deepcopy(adapter.environment)

        for name, function in (
            ("recover", recover),
            ("check_resources", resources),
            ("check_kernel", kernel),
            ("check_controller", controller),
            ("evaluate", evaluate),
        ):
            monkeypatch.setattr(adapter, name, function)

        state.enqueue = lambda: queued(
            user, gpu_version, evaluation_fingerprint=adapter.fingerprint
        )
        state.run = lambda **kwargs: run_worker(
            guard,
            state.worker_id,
            adapter,
            receipt_directory=state.directory,
            stop=state.stop,
            **kwargs,
        )
        yield state


def inactive(worker, *, retained=None):
    device = GPUDevice.objects.get(pk=worker.guard.device_uuid)
    assert device.state == "quarantined" and device.checked_at is None
    assert device.job_id == retained
    assert not worker.path.exists()
    assert not WorkerHealth.objects.exists()


@pytest.mark.parametrize("with_job", [False, True])
def test_once_always_checks_all_stages_and_revokes_readiness(worker, with_job):
    job = worker.enqueue() if with_job else None
    worker.run(once=True)
    assert [event for event in worker.events if type(event) is str][:24] == list(CHECKS)
    assert worker.events.count("execute") == int(with_job)
    if job:
        job.refresh_from_db()
        assert job.status == "finished" and job.verdict == "wrong_answer"
        assert job.budget_state == "consumed"
    inactive(worker)
    worker.guard.assert_held()  # Caller releases only AFTER shutdown.


def test_restart_never_reuses_previous_startup_results(worker):
    worker.run(once=True)
    worker.run(once=True)
    assert worker.events.count("resources") == 2
    assert worker.events.count("controller/service-deadline") == 2
    inactive(worker)


@pytest.mark.parametrize("deleted", [False, True])
def test_cancel_or_delete_recovers_before_next_job(worker, deleted):
    first, second = worker.enqueue(), worker.enqueue()
    first_id = first.id

    def execute(service, _kwargs):
        if service.job_id == first_id:
            if deleted:
                first.delete()
            else:
                Submission.objects.filter(pk=first_id).update(
                    status="finished",
                    verdict="internal_error",
                    lease_until=None,
                    result={"label": "Synthetic cancellation"},
                    finished_at=timezone.now(),
                )
        else:
            assert worker.events.count("initial-recovery") == 2
            assert ("stop", "task", first_id) in worker.events

    worker.execute_hook = execute
    waits = []

    def wait(_seconds):
        waits.append(1)
        assert len(waits) <= 3, "Worker failed to recover or claim the next job"
        if Submission.objects.get(pk=second.id).status == "finished":
            worker.stop.set()

    worker.stop.wait = wait
    worker.run()
    second.refresh_from_db()
    assert second.verdict == "wrong_answer" and second.budget_state == "consumed"
    if not deleted:
        first.refresh_from_db()
        assert first.result == {"label": "Synthetic cancellation"}
        assert first.budget_state == "consumed"
    inactive(worker)


@pytest.mark.parametrize("stage", ["initial-recovery", "resources", "controller/service-deadline"])
def test_stop_during_startup_never_claims_or_leaves_a_receipt(worker, stage):
    job = worker.enqueue()
    worker.phase_hook = lambda name: worker.stop.set() if name == stage else None
    worker.run()
    job.refresh_from_db()
    assert job.status == "queued" and "execute" not in worker.events
    inactive(worker)


def test_stop_during_task_keeps_reservation_for_next_complete_startup(worker):
    job = worker.enqueue()
    worker.execute_hook = lambda _service, _kwargs: worker.stop.set()
    worker.run()
    job.refresh_from_db()
    assert job.status == "running" and job.budget_state == "consumed"
    assert ("stop", "task", job.id) in worker.events
    inactive(worker, retained=job.id)
    worker.stop.clear()
    worker.execute_hook = None
    worker.run(once=True)
    job.refresh_from_db()
    assert job.status == "finished" and job.verdict == "internal_error"
    assert job.budget_state == "consumed"
    inactive(worker)


def test_cleanup_identity_failure_never_constructs_replacement_service(worker):
    job = worker.enqueue()

    def execute(service, _kwargs):
        def refuse():
            worker.events.append("original-stop-refused")
            raise SandboxUnavailable("fixed invocation mismatch")

        service.stop = refuse
        Submission.objects.filter(pk=job.id).update(lease_until=timezone.now())

    worker.execute_hook = execute
    with pytest.raises(SandboxUnavailable, match="invocation mismatch"):
        worker.run(once=True)
    assert worker.events.count("original-stop-refused") == 1
    assert worker.events.count("initial-recovery") == 1
    inactive(worker, retained=job.id)


def test_cancel_recovery_refuses_replaced_slot_before_touching_services(worker, monkeypatch):
    job = worker.enqueue()
    replacement = uuid.uuid4()
    worker.execute_hook = lambda _service, _kwargs: Submission.objects.filter(pk=job.id).update(
        lease_until=timezone.now()
    )
    original = gpu_worker.recover_cancelled

    def changed(*args, **kwargs):
        GPUDevice.objects.filter(pk=worker.guard.device_uuid).update(
            job_id=replacement, reservation_token=uuid.uuid4()
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(gpu_worker, "recover_cancelled", changed)
    with pytest.raises(DeviceOwnershipError, match="before recovery"):
        worker.run(once=True)
    assert not any(type(event) is tuple and event[-1] == replacement for event in worker.events)
    assert worker.events.count("initial-recovery") == 1
    inactive(worker, retained=replacement)


def test_failed_cancel_recovery_cannot_free_slot_or_claim_next(worker):
    job, next_job = worker.enqueue(), worker.enqueue()
    worker.execute_hook = lambda _service, _kwargs: Submission.objects.filter(pk=job.id).update(
        lease_until=timezone.now()
    )

    def phase(name):
        if name == "initial-recovery" and worker.events.count(name) == 2:
            raise SandboxUnavailable("fixed failed recovery")

    worker.phase_hook = phase
    with pytest.raises(SandboxUnavailable, match="failed recovery"):
        worker.run()
    next_job.refresh_from_db()
    assert next_job.status == "queued"
    inactive(worker, retained=job.id)


def test_idle_near_expiry_runs_full_suite_again(worker, monkeypatch):
    original = gpu_worker.claim_ready
    calls = []

    def claim(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            with ReceiptStore(worker.directory, worker.guard) as store:
                value = store.read()
            with monkeypatch.context() as patch:
                patch.setattr(
                    gpu_receipt.time,
                    "time",
                    lambda: value["created_wall"] + gpu_receipt.TTL_SECONDS - 100,
                )
                return original(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(gpu_worker, "claim_ready", claim)
    worker.run(once=True)
    assert len(calls) == 2 and worker.events.count("resources") == 2
    inactive(worker)


@pytest.mark.parametrize("defect", ["missing", "expired", "worker", "policy"])
def test_inflight_readiness_change_discards_result(worker, monkeypatch, defect):
    job = worker.enqueue()

    def execute(_service, _kwargs):
        if defect == "missing":
            worker.path.unlink()
        elif defect == "expired":
            fixed = gpu_receipt.time.time() + gpu_receipt.TTL_SECONDS + 1
            monkeypatch.setattr(gpu_receipt.time, "time", lambda: fixed)
        elif defect == "worker":
            GPUDevice.objects.filter(pk=worker.guard.device_uuid).update(worker_id="changed")
        else:
            monkeypatch.setattr(
                gpu_receipt, "environment_identity", lambda *_args: {"changed": True}
            )

    worker.execute_hook = execute
    with pytest.raises((SandboxUnavailable, FileNotFoundError, DeviceOwnershipError)):
        worker.run(once=True)
    job.refresh_from_db()
    assert job.status == "running" and job.budget_state == "consumed"
    inactive(worker, retained=job.id)


def test_receipt_removed_after_adapter_return_cannot_commit_result(worker, monkeypatch):
    job = worker.enqueue()
    original = worker.adapter.evaluate

    def evaluated(*args, **kwargs):
        result = original(*args, **kwargs)
        worker.path.unlink()
        return result

    monkeypatch.setattr(worker.adapter, "evaluate", evaluated)
    with pytest.raises(FileNotFoundError):
        worker.run(once=True)
    job.refresh_from_db()
    assert job.status == "running" and not job.verdict
    inactive(worker, retained=job.id)


def test_stop_between_health_and_claim_rolls_back_claim(worker, monkeypatch):
    from core import gpu_resources

    job = worker.enqueue()
    original = gpu_resources.claim

    def claim(guard):
        result = original(guard)
        worker.stop.set()
        return result

    monkeypatch.setattr(gpu_resources, "claim", claim)
    worker.run()
    job.refresh_from_db()
    assert job.status == "queued" and job.lease_token is None
    assert "execute" not in worker.events
    inactive(worker)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_owned_process_signal_stops_wait_and_restores_handlers(signum):
    # Trusted fixed control code only; no submissions, CUDA or service changes.
    program = """
import os, signal, threading
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'praximesh.settings')
import django
django.setup()
from core.gpu_worker import StopFlag, stop_signals
stop = StopFlag()
previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
with stop_signals(stop), threading.Lock():
    print('ready', flush=True)
    assert stop.wait(8)
assert all(signal.getsignal(n) == handler for n, handler in previous.items())
print('stopped', flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        with DefaultSelector() as selector:
            selector.register(child.stdout, EVENT_READ)
            assert selector.select(8), "Owned signal test did not start"
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signum)
        output, errors = child.communicate(timeout=8)
        assert child.returncode == 0 and output.strip() == "stopped" and not errors
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)


def test_signal_context_restores_handlers_on_exception():
    before = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    with pytest.raises(ValueError), stop_signals(StopFlag()):
        raise ValueError("fixed")
    assert all(signal.getsignal(n) == handler for n, handler in before.items())


def test_signal_handler_rejects_lock_based_thread_events():
    with pytest.raises(ValueError, match="lock-free"):
        with stop_signals(Event()):
            pytest.fail("A signal handler must not reenter a thread event lock")


@pytest.mark.parametrize(
    "defect",
    [
        "disabled",
        "gpu",
        "worker",
        "podman",
        "directory",
        "archive",
        "container-pair",
        "socket-pair",
        "container-new",
    ],
)
def test_command_rejects_invalid_config_before_device_access(
    tmp_path, settings, monkeypatch, defect
):
    from core.management.commands import gpu_worker as command

    settings.JUDGE_ENABLED = defect != "disabled"
    archive = tmp_path / "synthetic-archive"
    archive.touch()
    options = dict(
        gpu="GPU-11111111-2222-3333-4444-555555555555",
        worker_id="unit-test",
        podman_version="5.8.2",
        directory=str(tmp_path / "jobs"),
        lock_directory=str(tmp_path / "locks"),
        receipt_directory=str(tmp_path / "receipts"),
        runtime_archive=str(archive),
        image_archive=str(archive),
        nvrtc_archive=str(archive),
    )
    if defect == "gpu":
        options["gpu"] = "0"
    elif defect == "worker":
        options["worker_id"] = "invalid worker"
    elif defect == "podman":
        options["podman_version"] = "latest"
    elif defect == "directory":
        options["directory"] = "relative"
    elif defect == "archive":
        options["nvrtc_archive"] += "-missing"
    elif defect == "container-pair":
        options["container_journal"] = str(tmp_path / "journal")
    elif defect == "socket-pair":
        options["docker_socket"] = str(tmp_path / "docker.sock")
    elif defect == "container-new":
        options.update(
            container_journal=str(tmp_path / "journal"), docker_socket=str(tmp_path / "docker.sock")
        )
    monkeypatch.setattr(command, "identify", lambda *_args: pytest.fail("Must not access device"))
    with pytest.raises(CommandError):
        call_command("gpu_worker", **options)
    assert not (tmp_path / "locks").exists()


def test_clock_corruption_is_fatal_even_when_other_clock_expired(worker):
    value = {
        **binding(worker.guard, worker.worker_id, worker.adapter),
        "created_wall": gpu_receipt.time.time() - gpu_receipt.TTL_SECONDS - 1,
        "created_monotonic": gpu_receipt.time.monotonic() + 10,
        "evidence_digest": "e" * 64,
    }
    with pytest.raises(SandboxUnavailable) as failure:
        gpu_receipt.validate_receipt(value, worker.guard, worker.worker_id, worker.adapter)
    assert not isinstance(failure.value, GPUReceiptExpired)


@pytest.mark.parametrize("failing", [False, True])
@pytest.mark.parametrize("with_journal", [False, True])
def test_command_owns_lock_through_shutdown_and_sanitizes_failures(
    worker, tmp_path, settings, monkeypatch, caplog, failing, with_journal
):
    from core.management.commands import gpu_worker as command

    settings.JUDGE_ENABLED = True
    archive = tmp_path / "synthetic-archive"
    archive.touch()
    if with_journal:
        engine = Engine()
        with ContainerJournal(tmp_path / "journal", worker.guard, engine):
            pass
        monkeypatch.setattr(command, "LocalEngine", lambda _path: engine)
    worker.guard.close()
    monkeypatch.setattr(command, "identify", lambda _uuid: copy.deepcopy(worker.adapter.target))
    monkeypatch.setattr(command, "GPUExecutionAdapter", lambda **_kwargs: worker.adapter)
    original = command.run_worker

    def run(guard, *args, **kwargs):
        guard.assert_held()
        if with_journal:
            assert kwargs["container_journal"].guard is guard
            kwargs["container_journal"].check()
        try:
            return original(guard, *args, **kwargs)
        finally:
            guard.assert_held()

    monkeypatch.setattr(command, "run_worker", run)
    if failing:

        def fail(_name):
            raise ValueError("synthetic-private-source-and-credential")

        worker.phase_hook = fail
    options = dict(
        gpu=worker.guard.device_uuid,
        worker_id=worker.worker_id,
        podman_version="5.8.2",
        directory=str(worker.adapter.directory),
        lock_directory=str(worker.guard.directory),
        receipt_directory=str(worker.directory),
        runtime_archive=str(archive),
        image_archive=str(archive),
        nvrtc_archive=str(archive),
        once=True,
    )
    if with_journal:
        options.update(
            container_journal=str(tmp_path / "journal"),
            docker_socket=str(tmp_path / "modeled.sock"),
        )
    if failing:
        with pytest.raises(CommandError) as error:
            call_command("gpu_worker", **options)
        assert str(error.value) == "GPU worker stopped: ValueError"
    else:
        call_command("gpu_worker", **options)
    assert "synthetic-private-source-and-credential" not in caplog.text
    inactive(worker)
    with DeviceLock(worker.guard.directory, worker.guard.device_uuid) as replacement:
        replacement.assert_held()


def test_worker_keeps_optional_journal_through_startup_and_cancel_recovery(worker, tmp_path):
    job = worker.enqueue()
    with ContainerJournal(tmp_path / "journal", worker.guard, Engine()) as journal:

        def expire(service, _kwargs):
            intent(journal, job.id, service.reservation_token)
            Submission.objects.filter(pk=job.id).update(lease_until=timezone.now())

        worker.execute_hook = expire
        worker.run(once=True, container_journal=journal)
        assert journal.load() is None and journal.engine.state is None
        assert len(list(journal.directory.glob("*.done.json"))) == 1
    assert worker.events.count("initial-recovery") == 2
    job.refresh_from_db()
    assert job.status == "finished" and job.verdict == "internal_error"
    inactive(worker)
