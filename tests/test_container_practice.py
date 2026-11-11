"""Real private state/locks, full startup with explicit GPU models; no database."""

import copy
import json
import os
import select
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from judge import container_practice as module
from judge.container_contract import WORKLOAD_PROFILE, docker_resources, resource_marker
from judge.container_devices import docker_devices, encoded_selection
from judge.container_execution import KernelTask
from judge.container_gpu import GPUContainerService
from judge.container_isolation_protocol import target_identity
from judge.container_journal import ContainerJournal
from judge.container_practice_state import KERNEL, MARKER, OPERATION, LocalPracticeState, inode
from judge.container_service import PREFIX, digest
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from judge.gpu_receipt import CHECKS
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_isolation import resource_report
from tests.test_container_journal import Engine
from tests.test_container_kernel_checks import check_report
from tests.test_container_lifecycle import cleanup
from tests.test_container_lifecycle import report as fault_report
from tests.test_container_supervision import snapshot
from tests.test_gpu_public_execution import public_completion
from tests.test_gpu_public_execution import public_problem as public_problem


@pytest.fixture
def local(configured, tmp_path, monkeypatch):
    root = tmp_path / "local"
    root.mkdir(mode=0o700)
    adapter, control = configured.adapter, configured.control
    engine = Engine()
    with DeviceLock(root / "locks", adapter.target["uuid"]) as guard:
        with ContainerJournal(root / "journal", guard, engine) as journal:
            control.guard, control.journal = guard, journal
            control.saved.update(
                root=inode(root.stat()),
                locks=inode(guard.directory.stat()),
                journal=inode(journal.directory.stat()),
                engine=engine.identifier,
            )
            (root / "mounts.json").write_text(json.dumps(control.saved))
            (root / "mounts.json").chmod(0o600)
            state = SimpleNamespace(
                root=root,
                adapter=adapter,
                control=control,
                journal=journal,
                guard=guard,
                engine=engine,
                tasks=configured.tasks,
                events=[],
                progress=[],
                fail=None,
                public_calls=0,
                observations=0,
            )

            class Monitor:
                def __init__(self, value):
                    assert value is control

                def observe(self):
                    state.observations += 1
                    assert journal.load() is None

            monkeypatch.setattr(module, "ContainerDeviceMonitor", Monitor)
            original_create = KernelTask.__init__

            def create(task, *args, **kwargs):
                original_create(task, *args, **kwargs)
                task.cleanup = task.status = None

            monkeypatch.setattr(KernelTask, "__init__", create)

            def run(task, request, *, deadline, on_tick, on_begin):
                operation = journal.read(OPERATION)
                assert operation["phase"] == "pending"
                assert str(task.service.job_id) == operation["job"]
                assert str(task.service.reservation_token) == operation["reservation"]
                now = time.monotonic()
                # Subtracting a frozen fractional clock can round a valid
                # 180-second budget upward; compare the original deadline.
                assert now < deadline <= now + 180
                assert journal.load() is None
                on_tick()
                on_begin()
                on_tick()
                if request["event"] == "recover":
                    name = "fresh-recovery" if state.events else "initial-recovery"
                elif request["event"] == "execute-public":
                    name = "public"
                    assert state.progress == list(CHECKS)
                    state.public_calls += 1
                elif request["event"] == "check-resources":
                    name = "resources"
                else:
                    name = request["event"].removeprefix("check-") + "/" + request["name"]
                state.events.append(name)
                if state.fail == name:
                    raise SandboxUnavailable("fixed local stage failure")
                task.service.container_id = uuid.uuid4().hex + uuid.uuid4().hex
                task.status = {"nonce": uuid.uuid4().hex}
                task.cleanup = cleanup(
                    task.service.container_id, task.status["nonce"], guard.device_uuid
                )
                completed = {
                    "artifacts": {
                        "image": adapter.image,
                        "task_contract": adapter.environment["artifacts"]["task_contract"],
                        "resource_profile": adapter.environment["resource_profile"],
                        "limits": copy.deepcopy(adapter.environment["workload_limits"]),
                    },
                }
                if name == "resources":
                    report = resource_report(request, gpu=True)
                    report["checks"][0]["details"]["guest"]["gpu_nodes"] = target_identity(
                        adapter.target, configured.selection
                    )[1]
                    completed["report"] = report
                elif name.startswith("kernel/"):
                    completed["report"] = check_report(request)
                elif name.startswith("controller/"):
                    report = fault_report(request, device=guard.device_uuid)
                    report.update(
                        container_id=task.service.container_id,
                        nonce=task.status["nonce"],
                        cleanup=copy.deepcopy(task.cleanup),
                    )
                    completed["report"] = report
                elif name == "public":
                    assert task.service.purpose == "task"
                    completed.update(public_completion(request))
                return completed

            configured.run = run
            yield state


def execute(local, state, public_problem, **kwargs):
    return module.execute_public(
        local.adapter,
        state,
        public_problem,
        "opaque source",
        on_tick=kwargs.pop("on_tick", lambda: None),
        on_progress=kwargs.pop("on_progress", local.progress.append),
        **kwargs,
    )


def test_full_public_operation_persists_before_work_and_returns_after_cleanup(
    local, public_problem
):
    with LocalPracticeState(local.journal, create=True) as state:
        result = execute(local, state, public_problem)
        assert result["formal_result"] is False and result["ready"] is False
        assert result["startup_checks"] == 24 and result["result"]["verdict"] == "accepted"
        assert "performance" not in result["result"]
        assert state.operation()["phase"] == "completed"
        assert local.events[-1] == "public" and local.public_calls == 1
        assert local.observations == 2 and local.journal.load() is None
        assert len(local.tasks) == 28
        old = state.operation()
        local.events.clear()
        local.progress.clear()
        execute(local, state, public_problem)
        assert local.progress == list(CHECKS)
        assert state.operation()["job"] != old["job"]
        assert state.operation()["reservation"] != old["reservation"]
    assert set(p.name for p in local.root.iterdir()) == {"locks", "journal", "mounts.json"}


@pytest.mark.parametrize("stage", [*CHECKS, "public"])
def test_failure_retains_pending_operation_and_requires_explicit_recovery(
    local, public_problem, stage
):
    with LocalPracticeState(local.journal, create=True) as state:
        local.fail = stage
        with pytest.raises(SandboxUnavailable, match="stage failure"):
            execute(local, state, public_problem)
        original = state.operation()
        assert original["phase"] == "pending"
        count = len(local.tasks)
        with pytest.raises(SandboxUnavailable, match="Recover the pending"):
            execute(local, state, public_problem)
        assert len(local.tasks) == count and state.operation() == original
        assert local.public_calls == int(stage == "public")
        assert state.recover()["ready"] is False
        assert state.operation()["phase"] == "recovered"


@pytest.mark.parametrize(
    "clock_start",
    [None, 934.0000000000003],
    ids=["current-clock", "fractional-clock-boundary"],
)
@pytest.mark.parametrize("change", ["cancel", "environment", "marker", "operation", "deadline"])
def test_late_startup_change_prevents_sending_learner_source(
    local, public_problem, change, clock_start, monkeypatch
):
    with LocalPracticeState(local.journal, create=True) as state:
        clock = [time.monotonic() if clock_start is None else clock_start]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

        def progress(name):
            local.progress.append(name)
            if name != CHECKS[-1]:
                return
            if change == "cancel":
                raise InterruptedError("cancelled")
            if change == "environment":
                local.adapter.environment["target"]["model"] = "changed"
            elif change == "deadline":
                clock[0] += 5000
            else:
                filename = MARKER if change == "marker" else OPERATION
                value = local.journal.read(filename)
                value["scope"] = "different"
                local.journal.write(filename, value)

        with pytest.raises((SandboxUnavailable, InterruptedError)):
            execute(local, state, public_problem, on_progress=progress)
        assert local.progress == list(CHECKS)
        assert local.public_calls == 0


def test_result_is_withheld_if_terminal_operation_write_fails(local, public_problem, monkeypatch):
    with LocalPracticeState(local.journal, create=True) as state:
        original = local.journal.write

        def write(name, value, **kwargs):
            if name == OPERATION and value["phase"] == "completed":
                raise OSError("fixed publication failure")
            return original(name, value, **kwargs)

        monkeypatch.setattr(local.journal, "write", write)
        with pytest.raises(OSError, match="publication"):
            execute(local, state, public_problem)
        assert local.public_calls == 1 and state.operation()["phase"] == "pending"


def test_invalid_public_data_never_registers_or_executes_a_local_task(local, public_problem):
    with LocalPracticeState(local.journal, create=True) as state:
        public_problem["reference_solution"] = "private"
        with pytest.raises(ValueError):
            execute(local, state, public_problem)
        assert state.operation() is None and local.tasks == []


def test_cancellation_after_terminal_write_still_withholds_the_result(
    local, public_problem, monkeypatch
):
    with LocalPracticeState(local.journal, create=True) as state:
        completed = False
        original = state.finish

        def finish(*args, **kwargs):
            nonlocal completed
            value = original(*args, **kwargs)
            completed = True
            return value

        def cancel():
            if completed:
                raise InterruptedError("cancelled after terminal publication")

        monkeypatch.setattr(state, "finish", finish)
        with pytest.raises(InterruptedError, match="terminal publication"):
            execute(local, state, public_problem, on_tick=cancel)
        assert state.operation()["phase"] == "completed"
        assert local.journal.load() is None


@pytest.mark.parametrize("name", ["worker.json", "receipts", "deployment-owner.json", "unknown"])
def test_local_initialization_does_not_adopt_server_or_unknown_state(local, name):
    path = local.root / name
    path.write_bytes(b"existing")
    with pytest.raises(SandboxUnavailable, match="standalone state"):
        LocalPracticeState(local.journal, create=True)
    assert path.read_bytes() == b"existing"
    assert local.journal.read(MARKER) is None


def test_initialization_does_not_adopt_unknown_journal_history(local):
    local.journal.write("previous.done.json", {"unknown": True})
    with pytest.raises(SandboxUnavailable, match="unused original"):
        LocalPracticeState(local.journal, create=True)
    assert local.journal.read("previous.done.json") == {"unknown": True}


def test_same_physical_lock_is_required_and_installation_cannot_be_copied(local):
    with LocalPracticeState(local.journal, create=True) as state:
        with pytest.raises(DeviceLockError):
            DeviceLock(local.guard.directory, local.guard.device_uuid).acquire()
        original = state.marker
    with LocalPracticeState(local.journal) as state:
        assert state.marker == original
        raw = (local.root / "mounts.json").read_bytes()
        (local.root / "mounts.json").write_bytes(raw + b" ")
        with pytest.raises(SandboxUnavailable, match="ownership changed"):
            state.check()


def local_intent(local, operation, *, purpose="task", job=None, command=None):
    service = GPUContainerService(
        local.journal,
        uuid.UUID(job or operation["job"]),
        uuid.UUID(operation["reservation"]),
        image=operation["environment"]["artifacts"]["task_image"],
        driver_manifest=local.adapter.driver_manifest,
        device_spec=operation["environment"]["device_spec"],
        purpose=purpose,
    )
    service.image_env = []
    value = snapshot(service)
    value["Image"] = service.image
    value["Config"]["Cmd"][3] = resource_marker(WORKLOAD_PROFILE)
    value["Config"]["Cmd"][4:] = command or [*KERNEL, encoded_selection(service.device_spec)]
    value["Config"]["Labels"].update(service.required_image_labels())
    value["Config"]["Labels"][PREFIX + "command"] = digest(value["Config"]["Cmd"])
    value["HostConfig"].update(
        docker_resources(WORKLOAD_PROFILE), Devices=docker_devices(service.device_spec)
    )
    service.record_intent(value["Config"]["Cmd"])
    value["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(local.journal.load()["security"][1]),
    ]
    local.engine.state = value
    return service


@pytest.mark.parametrize("purpose", ["maintenance", "task"])
def test_recovery_removes_only_matching_original_container_and_never_starts_image(local, purpose):
    with LocalPracticeState(local.journal, create=True) as state:
        operation = state.begin(local.adapter.environment)
        local_intent(local, operation, purpose=purpose)
        response = state.recover()
        assert response["cleanup"]["removed"] and response["ready"] is False
        assert state.operation()["phase"] == "recovered"
        assert local.engine.state is None and local.journal.load() is None
        assert state.recover()["status"] == "idle"
        assert not any(
            call[:2] in {("container", "create"), ("container", "start")}
            for call in local.engine.calls
        )


@pytest.mark.parametrize("defect", ["job", "command", "completed", "unregistered"])
def test_unowned_container_is_not_stopped_or_deleted(local, defect):
    with LocalPracticeState(local.journal, create=True) as state:
        operation = state.begin(local.adapter.environment)
        if defect == "completed":
            state.finish(operation)
        local_intent(
            local,
            operation,
            job=str(uuid.uuid4()) if defect == "job" else None,
            command=["/usr/bin/sleep", "30"] if defect == "command" else None,
        )
        if defect == "unregistered":
            os.unlink(OPERATION, dir_fd=local.journal.fd)
        original = copy.deepcopy(local.engine.state)
        with pytest.raises(SandboxUnavailable):
            state.recover()
        assert local.engine.state == original and local.journal.load() is not None
        assert not any(
            call[:2] in {("container", "kill"), ("container", "rm")} for call in local.engine.calls
        )


@pytest.mark.parametrize("point", ["pending", "intent", "completed"])
def test_actual_process_kill_preserves_physical_lock_and_original_operation(local, point):
    # This child writes real files while holding the real lock. Its engine exposes
    # only a synthetic daemon UUID; it cannot create, query or run a GPU container.
    script = r"""
import json, sys, time, uuid
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from judge.container_contract import WORKLOAD_PROFILE, resource_marker
from judge.container_devices import encoded_selection
from judge.container_gpu import GPUContainerService
from judge.container_journal import ContainerJournal
from judge.container_practice_state import KERNEL, LocalPracticeState
from judge.gpu_device_lock import DeviceLock
class Engine:
    def verify(self): pass
    def run(self, *args, **kwargs):
        assert args == ('info', '--format', '{{.ID}}')
        return SimpleNamespace(returncode=0, stdout=sys.argv[4].encode())
root, device, point = Path(sys.argv[2]), sys.argv[3], sys.argv[5]
environment, driver = json.loads(sys.stdin.readline())
with DeviceLock(root/'locks', device) as guard:
    with ContainerJournal(root/'journal', guard, Engine()) as journal:
        with LocalPracticeState(journal) as state:
            operation = state.begin(environment)
            if point == 'intent':
                service = GPUContainerService(journal, uuid.UUID(operation['job']), uuid.UUID(operation['reservation']), image=environment['artifacts']['task_image'], driver_manifest=driver, device_spec=environment['device_spec'])
                service.image_env = []
                now = time.monotonic()
                service.record_intent([str(now+5), str(now+180), uuid.uuid4().hex, resource_marker(WORKLOAD_PROFILE), *KERNEL, encoded_selection(environment['device_spec'])])
            elif point == 'completed':
                state.finish(operation)
            print(point, flush=True)
            while True: time.sleep(1)
"""
    from pathlib import Path

    with LocalPracticeState(local.journal, create=True) as state:
        marker = state.marker
    original_inode = inode((local.guard.directory / local.guard.filename).stat())
    local.guard.close()
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path.cwd()),
            str(local.root),
            local.guard.device_uuid,
            local.engine.identifier,
            point,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
    )
    try:
        process.stdin.write(
            json.dumps([local.adapter.environment, local.adapter.driver_manifest]).encode() + b"\n"
        )
        process.stdin.close()
        assert select.select([process.stdout], [], [], 15)[0], (
            "Owned child did not reach its durable stage"
        )
        assert process.stdout.readline().decode().strip() == point
        assert process.poll() is None
        with pytest.raises(DeviceLockError):
            DeviceLock(local.guard.directory, local.guard.device_uuid).acquire()
        process.kill()
        assert process.wait(timeout=5) == -9
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        local.guard.acquire()
    assert inode((local.guard.directory / local.guard.filename).stat()) == original_inode
    with LocalPracticeState(local.journal) as state:
        assert state.marker == marker
        operation = state.operation()
        if point != "completed":
            with pytest.raises(SandboxUnavailable, match="Recover the pending"):
                state.begin(local.adapter.environment)
        response = state.recover()
        assert response["status"] == ("idle" if point == "completed" else "recovered")
        assert response["ready"] is False and local.journal.load() is None
        assert state.operation()["job"] == operation["job"]


def test_production_lifecycle_import_does_not_load_django():
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); import judge.container_practice; assert not any(n == 'django' or n.startswith('django.') or n == 'core' or n.startswith('core.') for n in sys.modules)",
            str(Path.cwd()),
        ],
        capture_output=True,
        timeout=10,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
    )
    assert result.returncode == 0, result.stderr.decode()


def test_recovery_keeps_frozen_task_identity_when_next_execution_uses_a_new_image(local):
    with LocalPracticeState(local.journal, create=True) as state:
        original = state.begin(local.adapter.environment)
        marker = copy.deepcopy(state.marker)
        local_intent(local, original)
        upgraded = copy.deepcopy(local.adapter.environment)
        upgraded["artifacts"]["task_image"] = "sha256:" + "e" * 64
        response = state.recover()
        archived = local.journal.read(response["cleanup"]["history"])
        assert archived["image"] == original["environment"]["artifacts"]["task_image"]
        assert state.begin(upgraded)["environment"] == upgraded
        assert state.marker == marker
        assert local.journal.read(response["cleanup"]["history"]) == archived
