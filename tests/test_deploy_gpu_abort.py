"""Original files/locks and SIGKILL; explicit Docker/database models, no GPU."""

import copy
import os
import signal
import time
from types import SimpleNamespace

import pytest

from judge.container_deployment_config import digest
from scripts.deploy_gpu_abort import GPUStartupAbortError, StartupAbort, maybe_abort
from scripts.deploy_gpu_candidate import attempt as preparation_attempt
from scripts.deploy_gpu_config import context
from scripts.deploy_gpu_recovery import attempt as recovery_attempt
from scripts.deploy_gpu_worker import ZERO
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import state
from tests.test_deploy_gpu_quiescence import quiescence as quiescence
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.fixture
def aborting(quiescence, monkeypatch):
    q, item = quiescence, quiescence.item
    q.model.guard.close()
    q.model.guard = None

    def exited(data):
        value = data["containers"]["/" + item.generation["gpu_worker"]]["State"]
        value.update(
            Running=False, Pid=0, Status="exited", ExitCode=1, FinishedAt="2026-11-01T12:01:00Z"
        )

    q.model.change(exited)
    previous = copy.deepcopy(item.generation)
    previous["id"] = "3" * 12
    previous["database"] += "_old"
    previous["manifest"].pop("gpu")
    previous.pop("gpu_worker")
    key = item.token + ":" + item.generation["gpu_worker"]
    held = SimpleNamespace(value=False)

    def operation_lock():
        if not held.value:
            raise GPUStartupAbortError("Original database operation lock required")

    monkeypatch.setattr(item.database, "_require_operation_lock", operation_lock)
    host = q.coordinator()
    with host.locked():
        runtime = host.state["gpu_runtime"][item.generation["gpu_worker"]]
        check = host.state["gpu_runtime"][item.generation["gpu_worker"] + "-check"]
        host.state["current"] = previous
        host.state["operation"].update(
            previous=previous, candidate=item.generation, phase="recovering", exposure_target=None
        )
        prep = preparation_attempt()
        prep.update(
            phase="waiting",
            startup={"container": runtime["id"], "started_at": runtime["started_at"]},
        )
        host.state["gpu_preparations"] = {
            key: {
                "schema": 1,
                "operation": item.token,
                "generation": copy.deepcopy(item.generation),
                "release": str(item.root / "frozen-release"),
                "attempts": [prep],
            }
        }
        attempt = recovery_attempt(next(iter(ZERO)))
        attempt.update(
            started=runtime["started_at"],
            check_id=check["id"],
            check_before=next(iter(ZERO)),
            check_started=check["started_at"],
        )
        host.state["gpu_recoveries"] = {
            key: {
                "schema": 1,
                "operation": item.token,
                "generation": context(item.generation),
                "metadata_sha256": digest(runtime["metadata"]),
                "container": runtime["id"],
                "runtime_sha256": runtime["runtime_sha256"],
                "phase": "waiting",
                "attempts": [attempt],
            }
        }
        host.state["gpu_releases"] = {
            item.token: {
                "schema": 1,
                "operation": item.token,
                "previous": previous,
                "candidate": copy.deepcopy(item.generation),
                "phase": "recovering",
                "archives": {item.generation["id"]: str(item.root / "frozen-release")},
                "initial_content": False,
                "recoveries": [],
            }
        }
        host.save()

    def run():
        host = q.coordinator()
        with host.locked():
            held.value = True
            try:
                return maybe_abort(host, item.generation, previous)
            finally:
                held.value = False

    return SimpleNamespace(q=q, item=item, previous=previous, key=key, run=run, held=held)


@pytest.mark.parametrize("with_recovery", [False, True])
def test_exited_startup_can_be_frozen_without_ready_or_rewriting_history(aborting, with_recovery):
    value = aborting
    if not with_recovery:
        host = value.q.coordinator()
        with host.locked():
            host.state["gpu_recoveries"].pop(value.key)
            host.save()
    before = state(value.item)
    before_calls = len(value.q.model.read()["calls"])
    assert value.run() is True
    after = state(value.item)
    assert after["gpu_startup_aborts"][value.key]["phase"] == "aborted"
    for key in ("gpu_preparations", "gpu_recoveries", "current", "operation", "gpu_configuration"):
        assert after[key] == before[key]
    assert value.q.read()["frozen"]
    assert not any(
        call[:2] in (["container", "start"], ["container", "stop"], ["container", "rm"])
        for call in value.q.model.read()["calls"][before_calls:]
    )
    calls = list(value.q.model.read()["calls"])
    assert value.run() is True
    assert state(value.item)["gpu_startup_aborts"] == after["gpu_startup_aborts"]
    assert not any(
        call[:2] == ["container", "start"] for call in value.q.model.read()["calls"][len(calls) :]
    )


@pytest.mark.parametrize(
    "defect",
    ["gpu-target", "exposed", "different-current", "not-recovering", "live", "oom", "exit-code"],
)
def test_ineligible_failure_does_not_create_abort_or_mutate_containers(aborting, defect):
    value = aborting
    host = value.q.coordinator()
    with host.locked():
        if defect == "gpu-target":
            value.previous["manifest"]["gpu"] = {"model": True}
            host.state["operation"]["previous"] = value.previous
            host.state["current"] = value.previous
        elif defect == "exposed":
            host.state["operation"]["exposure_target"] = value.item.generation["id"]
        elif defect == "different-current":
            host.state["current"] = value.item.generation
        elif defect == "not-recovering":
            host.state["operation"]["phase"] = "active"
        host.save()
    if defect in {"live", "oom", "exit-code"}:

        def change(data):
            info = data["containers"]["/" + value.item.generation["gpu_worker"]]["State"]
            if defect == "live":
                info.update(Running=True, Pid=12345, Status="running")
            elif defect == "oom":
                info["OOMKilled"] = True
            else:
                info["ExitCode"] = 137

        value.q.model.change(change)
    calls = list(value.q.model.read()["calls"])
    assert value.run() is False
    assert not state(value.item).get("gpu_startup_aborts") and not value.q.read()["frozen"]
    assert not any(
        call[:2] in (["container", "start"], ["container", "stop"], ["container", "rm"])
        for call in value.q.model.read()["calls"][len(calls) :]
    )


@pytest.mark.parametrize(
    "defect",
    [
        "pending",
        "journal",
        "pause",
        "route",
        "recovery-metadata",
        "recovery-token",
        "release-target",
    ],
)
def test_unproven_state_is_retained_and_cannot_become_an_abort(aborting, defect):
    value = aborting
    if defect == "pending":
        value.item.model.drain = False
    elif defect == "pause":
        value.item.model.pause = False
    elif defect == "route":
        value.item.model.port = 12345
    elif defect == "journal":
        p = value.item.root / "journal" / "unknown.active.json"
        p.write_text("{}")
        p.chmod(0o600)
    else:
        host = value.q.coordinator()
        with host.locked():
            if defect == "recovery-metadata":
                host.state["gpu_recoveries"][value.key]["metadata_sha256"] = "e" * 64
            elif defect == "recovery-token":
                host.state["gpu_recoveries"][value.key]["operation"] = (
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                )
            else:
                host.state["gpu_releases"][value.item.token]["previous"]["id"] = "e" * 12
            host.save()
    with pytest.raises((ValueError, RuntimeError)):
        value.run()
    assert not state(value.item).get("gpu_startup_aborts") and not value.q.read()["frozen"]
    assert value.q.read()["events"] == []


def test_completed_proof_cannot_hide_changed_history_or_database(aborting):
    value = aborting
    value.run()
    original = state(value.item)
    host = value.q.coordinator()
    with host.locked():
        host.state["gpu_recoveries"][value.key]["attempts"][-1]["before"] = "2026-11-01T11:00:00Z"
        host.save()
    with pytest.raises(GPUStartupAbortError, match="history|intent"):
        value.run()
    assert state(value.item)["gpu_startup_aborts"] == original["gpu_startup_aborts"]


def test_abort_context_is_required_and_cannot_be_reused_outside_original_lock(aborting):
    value = aborting
    host = value.q.coordinator()
    with host.locked():
        value.held.value = True
        abort = StartupAbort(host, value.item.generation, value.previous)
        abort.record = abort.original() | {"phase": "prepared"}
        abort.save("prepared")
        with pytest.raises(GPUStartupAbortError, match="only original CPU rollback"):
            host.gpu_worker_runtime(value.item.generation, "start")
        with pytest.raises(ValueError, match="abort"):
            host.publish_gpu_configuration(value.item.generation, enabled=True)
        value.held.value = False
    value.run()


@pytest.mark.parametrize("defect", ["container", "startup", "runtime-metadata"])
def test_changed_original_instance_is_rejected_before_any_abort_intent(aborting, defect):
    value = aborting
    if defect == "runtime-metadata":
        host = value.q.coordinator()
        with host.locked():
            host.state["gpu_runtime"][value.item.generation["gpu_worker"]]["metadata"][
                "unknown"
            ] = True
            host.save()
    else:

        def change(data):
            info = data["containers"]["/" + value.item.generation["gpu_worker"]]
            if defect == "container":
                info["Id"] = "e" * 64
            else:
                info["State"]["StartedAt"] = "2026-11-01T12:02:00Z"

        value.q.model.change(change)
    with pytest.raises(RuntimeError):
        value.run()
    assert not state(value.item).get("gpu_startup_aborts") and value.q.read()["events"] == []


def test_arbitrary_abort_flag_cannot_bypass_recovery_guard(aborting):
    from scripts.deploy_gpu_config import require_stopped_workers
    from scripts.deploy_gpu_worker import WorkerRuntime

    value = aborting
    host = value.q.coordinator()
    with host.locked():
        runtime = WorkerRuntime(host, value.item.generation)
        with runtime.node.locked() as node:
            with pytest.raises(ValueError, match="original internal"):
                require_stopped_workers(host, node, runtime.binding, abort=True)


def test_completed_abort_allows_only_original_configuration_retirement(aborting):
    value = aborting
    value.run()
    before = state(value.item)
    host = value.q.coordinator()
    with host.locked():
        value.held.value = True
        try:
            retired = host.retire_gpu_configuration(value.item.generation)
        finally:
            value.held.value = False
    after = state(value.item)
    assert retired["phase"] == "retired"
    assert after["gpu_configuration"] == {"schema": 1, "active": None, "transition": None}
    for key in ("gpu_preparations", "gpu_recoveries", "gpu_startup_aborts", "current"):
        assert after[key] == before[key]
    assert value.q.read()["frozen"]


@pytest.mark.parametrize("defect", [None, "no-recovery", "candidate-target", "unfinished"])
def test_retired_abort_allows_next_operation_only_after_original_cpu_recovery(aborting, defect):
    from scripts.deploy_gpu_abort import completed_recovery_abort
    from scripts.deploy_gpu_worker import WorkerRuntime

    value = aborting
    value.run()
    host = value.q.coordinator()
    with host.locked():
        value.held.value = True
        try:
            host.retire_gpu_configuration(value.item.generation)
            release = host.state["gpu_releases"][value.item.token]
            release["phase"] = "active"
            release["recoveries"] = [
                {
                    "id": value.item.token,
                    "target": value.previous["id"],
                    "source": value.item.generation["id"],
                    "phase": "finished",
                }
            ]
            if defect == "no-recovery":
                release["recoveries"] = []
            elif defect == "candidate-target":
                release["recoveries"][-1]["target"] = value.item.generation["id"]
            elif defect == "unfinished":
                release["recoveries"][-1]["phase"] = "expose_target"
            host.save("rolled_back")
            before = copy.deepcopy(host.state)
            runtime = WorkerRuntime(host, value.item.generation)
            with runtime.node.locked() as node:
                recovery = host.state["gpu_recoveries"][value.key]
                if defect:
                    with pytest.raises(GPUStartupAbortError, match="history"):
                        completed_recovery_abort(host, node, runtime.binding, value.key, recovery)
                else:
                    assert completed_recovery_abort(
                        host, node, runtime.binding, value.key, recovery
                    )
            assert host.state == before
        finally:
            value.held.value = False


@pytest.mark.parametrize("phase", ["prepared", "stopped", "freezing", "aborted"])
def test_sigkill_retries_original_abort_without_starting_or_rewriting_failed_recovery(
    aborting, monkeypatch, phase
):
    value = aborting
    before = copy.deepcopy(state(value.item)["gpu_recoveries"])
    pid = os.fork()
    if pid == 0:
        save = StartupAbort.save

        def crash(self, current):
            save(self, current)
            if current == phase:
                os.kill(os.getpid(), signal.SIGKILL)

        try:
            monkeypatch.setattr(StartupAbort, "save", crash)
            value.run()
        except BaseException:
            os._exit(91)
        os._exit(92)
    deadline = time.monotonic() + 20
    while True:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found:
            break
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("Owned abort child did not reach checkpoint")
        time.sleep(0.02)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert value.run() is True
    assert state(value.item)["gpu_recoveries"] == before and value.q.read()["frozen"]
