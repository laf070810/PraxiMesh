"""Real lock/journal files and modeled GPU checks, without application DB access."""

import copy
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge import container_startup_suite as module
from judge.container_isolation_protocol import target_identity
from judge.container_journal import ContainerJournal
from judge.container_readiness import SCOPE, task_identity
from judge.gpu_device_lock import DeviceLock
from judge.gpu_receipt import CHECKS
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_isolation import resource_report
from tests.test_container_journal import Engine
from tests.test_container_kernel_checks import check_report
from tests.test_container_lifecycle import cleanup
from tests.test_container_lifecycle import report as fault_report


@pytest.fixture
def standalone(configured, tmp_path):
    adapter, control = configured.adapter, configured.control
    events = []
    with DeviceLock(tmp_path / "locks", adapter.target["uuid"]) as guard:
        with ContainerJournal(tmp_path / "journal", guard, Engine()) as journal:
            control.guard, control.journal = guard, journal
            state = SimpleNamespace(
                adapter=adapter,
                control=control,
                journal=journal,
                guard=guard,
                events=events,
                tasks=configured.tasks,
                fail=None,
                progress=[],
                job=uuid.uuid4(),
                reservation=uuid.uuid4(),
            )

            def run(task, request, *, deadline, on_tick, on_begin):
                assert task.service.purpose == "maintenance"
                assert task.service.job_id == state.job
                assert task.service.reservation_token == state.reservation
                assert 0 < deadline - time.monotonic() <= 180
                assert journal.load() is None
                task.cleanup = task.status = None
                on_tick()
                on_begin()
                if request["event"] == "recover":
                    name = "initial-recovery" if not events else "fresh-recovery"
                elif request["event"] == "check-resources":
                    name = "resources"
                else:
                    name = request["event"].removeprefix("check-") + "/" + request["name"]
                events.append(name)
                if state.fail == name:
                    raise SandboxUnavailable("fixed startup stage failure")
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
                return completed

            configured.run = run
            task = adapter.task(guard, state.job, state.reservation, purpose="maintenance")
            adapter.recover(task, deadline=time.monotonic() + 90, on_tick=lambda: None)
            state.initial = {
                "scope": SCOPE,
                "previous_job": None,
                "previous_reservation": None,
                "container_recovery": {"had_record": False, "cleanup": None, "ready": False},
                "maintenance": task_identity(task),
                "environment": copy.deepcopy(adapter.environment),
                "recovered": True,
                "ready": False,
            }

            def invoke(**kwargs):
                options = {
                    "deadline": time.monotonic() + 4320,
                    "on_tick": lambda: None,
                    "on_progress": state.progress.append,
                    **kwargs,
                }
                return module.run_startup(
                    adapter, state.initial, state.job, state.reservation, **options
                )

            state.run = invoke
            yield state


def test_full_sequence_uses_real_lock_without_database_or_readiness_publication(standalone):
    evidence = standalone.run()
    assert [row["name"] for row in evidence] == list(CHECKS)
    assert standalone.progress == list(CHECKS)
    assert [name for name in standalone.events if name != "fresh-recovery"] == list(CHECKS)
    assert len(standalone.tasks) == len(CHECKS) + 3
    assert standalone.journal.load() is None
    assert evidence[0]["report"]["ready"] is False
    assert evidence[0]["report"] is not standalone.initial
    assert all(row["report"]["task"]["cleanup"]["removed"] for row in evidence[1:])
    assert not (standalone.journal.directory.parent / "receipts").exists()


@pytest.mark.parametrize("stage", CHECKS[1:])
def test_failure_at_each_required_stage_stops_before_later_progress(standalone, stage):
    standalone.fail = stage
    with pytest.raises(SandboxUnavailable, match="stage failure"):
        standalone.run()
    assert standalone.progress == list(CHECKS[: CHECKS.index(stage)])
    assert stage == standalone.events[-1]


def test_cancellation_after_validated_progress_stops_next_task(standalone):
    class Cancelled(Exception):
        pass

    def progress(name):
        standalone.progress.append(name)
        if name == "kernel/vector-reference":
            raise Cancelled

    with pytest.raises(Cancelled):
        standalone.run(on_progress=progress)
    assert standalone.events[-1] == "kernel/vector-reference"


@pytest.mark.parametrize("damage", ["cleanup", "scope", "environment"])
def test_bad_initial_recovery_never_starts_a_new_task(standalone, damage):
    if damage == "cleanup":
        standalone.initial["maintenance"]["cleanup"]["removed"] = False
    elif damage == "scope":
        standalone.initial["scope"] = "saved-report"
    else:
        standalone.initial["environment"] = {}
    with pytest.raises(SandboxUnavailable):
        standalone.run()
    assert standalone.progress == [] and standalone.events == ["initial-recovery"]


@pytest.mark.parametrize("damage", ["owner", "environment", "deadline"])
def test_late_identity_or_deadline_change_withholds_complete_evidence(
    standalone, damage, monkeypatch
):
    clock = [time.monotonic()]
    deadline = clock[0] + 4320
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def progress(name):
        if name == CHECKS[-1]:
            if damage == "owner":
                standalone.control.guard = object()
            elif damage == "environment":
                standalone.adapter.environment["unexpected"] = True
            else:
                clock[0] = deadline + 0.1

    with pytest.raises(SandboxUnavailable):
        standalone.run(deadline=deadline, on_progress=progress)


def test_cleanup_failure_withholds_stage_progress(standalone, monkeypatch):
    original = standalone.adapter.check_resources

    def invalid(task, *args, **kwargs):
        report, environment = original(task, *args, **kwargs)
        task.cleanup["removed"] = False
        return report, environment

    monkeypatch.setattr(standalone.adapter, "check_resources", invalid)
    with pytest.raises(SandboxUnavailable):
        standalone.run()
    assert standalone.progress == ["initial-recovery"]


def test_new_process_can_import_suite_without_django_or_application_settings():
    script = """import sys
sys.path.insert(0, sys.argv[1])
from judge.container_startup_suite import run_startup
assert callable(run_startup)
assert not any(n == 'django' or n.startswith('django.') or n == 'core' or n.startswith('core.') for n in sys.modules)
print('database-independent import passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path.cwd())],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
