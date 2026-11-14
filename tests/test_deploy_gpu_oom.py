"""Real local socket/process identity, and explicit Docker lifecycle models."""

import copy
import json
import os
import signal
import socket
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy_gpu_oom import observe_daemon, runtime_fingerprint
from scripts.deploy_gpu_worker import WorkerLifecycleError, WorkerRuntime
from scripts.deploy_web import fingerprint
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import state
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


def test_kernel_socket_peer_binds_actual_process_and_oom_default(tmp_path):
    path = tmp_path / "engine.sock"
    identifier = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        engine = SimpleNamespace(
            path=path,
            check_socket=lambda: None,
            verify=lambda: {"ID": identifier, "OomKillDisable": False, "ServerVersion": "test"},
        )
        result = observe_daemon(engine)
        assert result["peer"] == [os.getpid(), os.getuid(), os.getgid()]
        assert result["engine"] == identifier
        assert result["oom_score_adj"] == int(Path("/proc/self/oom_score_adj").read_text())
        assert result["socket"] == [path.stat().st_dev, path.stat().st_ino]


@pytest.mark.parametrize("daemon_score", [-200, 0, 200])
def test_native_startup_normalization_keeps_effective_score_and_all_other_fields(
    lifecycle, daemon_score
):
    lifecycle.model.daemon_oom["oom_score_adj"] = daemon_score
    before = lifecycle.act("create", "check")
    record = state(lifecycle.item)["gpu_runtime"][before["Name"][1:]]
    assert record["schema"] == 2
    assert before["HostConfig"]["OomScoreAdj"] == max(0, daemon_score)
    assert before["HostConfig"]["OomKillDisable"] is False
    after = lifecycle.act("check", "check")
    assert after["HostConfig"]["OomScoreAdj"] == max(0, daemon_score)
    assert after["HostConfig"]["OomKillDisable"] is None
    assert fingerprint(before) != fingerprint(after)
    assert runtime_fingerprint(before, record) == runtime_fingerprint(after, record)
    assert record["runtime_sha256"] == runtime_fingerprint(after, record)
    lifecycle.act("stop", "check")
    lifecycle.act("check", "check")


@pytest.mark.parametrize("field", ["oom_score_adj", "start_ticks", "socket", "peer", "engine"])
def test_daemon_changes_cannot_refresh_creation_contract_or_start(lifecycle, field):
    created = lifecycle.act("create", "check")
    original = copy.deepcopy(state(lifecycle.item)["gpu_runtime"][created["Name"][1:]])
    value = lifecycle.model.daemon_oom[field]
    lifecycle.model.daemon_oom[field] = (
        value + 1
        if type(value) is int
        else [value[0] + 1, *value[1:]]
        if type(value) is list
        else str(uuid.uuid4())
    )
    with pytest.raises(WorkerLifecycleError, match="daemon OOM"):
        lifecycle.act("start", "check")
    assert not any(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"])
    assert state(lifecycle.item)["gpu_runtime"][created["Name"][1:]]["oom"] == original["oom"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("OomScoreAdj", 201),
        ("OomScoreAdj", True),
        ("OomKillDisable", True),
        ("OomKillDisable", 0),
        ("OomKillDisable", "false"),
        ("OomKillDisable", "missing"),
        ("MemorySwappiness", 40),
    ],
)
def test_effective_setting_or_other_metadata_change_is_rejected(lifecycle, field, value):
    info = lifecycle.act("check", "check")

    def corrupt(data):
        runtime = data["containers"][info["Name"]]["HostConfig"]
        if value == "missing":
            runtime.pop(field)
        else:
            runtime[field] = value

    lifecycle.model.change(corrupt)
    with pytest.raises(WorkerLifecycleError, match="creation contract"):
        lifecycle.act("stop", "check")


def old_failed_check(lifecycle, *, initial_stop=True):
    """Build the exact legacy writer output, then reproduce its failed first start."""
    lifecycle.act("create")
    lifecycle.act("create", "check")
    host = lifecycle.item.coordinator()
    data = lifecycle.model.read()
    with host.locked():
        for record in host.state["gpu_runtime"].values():
            record["schema"] = 1
            record.pop("oom")
            record["arguments"] = [
                a for a in record["arguments"] if not a.startswith("--oom-score-adj=")
            ]
            info = data["containers"][
                "/"
                + record["generation"]["gpu_worker"]
                + ("-check" if record["mode"] == "check" else "")
            ]
            info["HostConfig"]["OomScoreAdj"] = 0
            record["runtime_sha256"] = fingerprint(info)
            key = "gpu_checks" if record["mode"] == "check" else "gpu_workers"
            host.state[key][record["generation"]["id"]]["runtime_sha256"] = record["runtime_sha256"]
        host.save()
    lifecycle.model.save(data)
    if initial_stop:
        lifecycle.act("stop", "check")
    with pytest.raises(WorkerLifecycleError, match="creation contract"):
        lifecycle.act("start", "check")
    return copy.deepcopy(state(lifecycle.item))


def reconcile(lifecycle):
    host = lifecycle.item.coordinator()
    with host.locked():
        WorkerRuntime(host, lifecycle.item.generation, mode="check").reconcile_legacy_preflight()


@pytest.mark.parametrize("initial_stop", [False, True])
def test_original_legacy_check_can_only_be_retired_without_rewriting_its_identity(
    lifecycle, initial_stop
):
    original = old_failed_check(lifecycle, initial_stop=initial_stop)
    name = lifecycle.item.generation["gpu_worker"] + "-check"
    reconcile(lifecycle)
    proof = copy.deepcopy(state(lifecycle.item)["gpu_legacy_oom"][name])
    reconcile(lifecycle)
    assert state(lifecycle.item)["gpu_legacy_oom"][name] == proof
    assert state(lifecycle.item)["gpu_runtime"] == original["gpu_runtime"]
    with pytest.raises(WorkerLifecycleError, match="only permits retirement"):
        lifecycle.act("start", "check")
    stopped = lifecycle.act("stop", "check")
    reconcile(lifecycle)
    assert stopped["State"]["ExitCode"] == 0
    record = state(lifecycle.item)["gpu_runtime"][name]
    assert record["id"] == original["gpu_runtime"][name]["id"]
    assert record["runtime_sha256"] == original["gpu_runtime"][name]["runtime_sha256"]
    assert state(lifecycle.item)["gpu_legacy_oom"][name] == proof
    assert sum(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"]) == 1
    assert not any(call[:2] == ["container", "stop"] for call in lifecycle.model.read()["calls"])
    # Inventory must recognize the original retired check without weakening other objects.
    lifecycle.item.model.containers = {
        info["Id"]: info for info in lifecycle.model.read()["containers"].values()
    }
    lifecycle.item.publish(enabled=True)


@pytest.mark.parametrize(
    "defect", ["score", "killer", "unrelated", "failed", "worker-start", "daemon-version"]
)
def test_legacy_recovery_refuses_any_unproven_change(lifecycle, defect):
    old_failed_check(lifecycle)
    name = "/" + lifecycle.item.generation["gpu_worker"]

    def corrupt(data):
        check = data["containers"][name + "-check"]
        if defect == "score":
            check["HostConfig"]["OomScoreAdj"] += 1
        elif defect == "killer":
            check["HostConfig"]["OomKillDisable"] = True
        elif defect == "unrelated":
            check["Config"]["Domainname"] = "changed"
        elif defect == "failed":
            check["State"]["ExitCode"] = 1
        elif defect == "worker-start":
            data["containers"][name]["State"]["StartedAt"] = "2026-11-01T12:00:01Z"

    lifecycle.model.change(corrupt)
    if defect == "daemon-version":
        lifecycle.model.daemon_oom["version"] = "unknown"
    with pytest.raises(WorkerLifecycleError):
        reconcile(lifecycle)
    assert not state(lifecycle.item).get("gpu_legacy_oom")


def test_sigkill_after_legacy_proof_replays_same_identity_and_does_not_restart(
    lifecycle, monkeypatch
):
    from scripts.deploy import Deployment

    old_failed_check(lifecycle)
    pid = os.fork()
    if pid == 0:
        save = Deployment.save

        def die_after_save(host, *args, **kwargs):
            save(host, *args, **kwargs)
            if host.state.get("gpu_legacy_oom"):
                os.kill(os.getpid(), signal.SIGKILL)

        try:
            monkeypatch.setattr(Deployment, "save", die_after_save)
            reconcile(lifecycle)
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
            pytest.fail("Owned coordinator child did not reach the proof save")
        time.sleep(0.02)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    proof = json.dumps(state(lifecycle.item)["gpu_legacy_oom"], sort_keys=True)
    reconcile(lifecycle)
    lifecycle.act("stop", "check")
    assert json.dumps(state(lifecycle.item)["gpu_legacy_oom"], sort_keys=True) == proof
    assert sum(call[:2] == ["container", "start"] for call in lifecycle.model.read()["calls"]) == 1
