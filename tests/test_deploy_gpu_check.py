"""Real release bytes/files/CPU process interruption; explicit control/daemon/DB models."""

import copy
import hashlib
import json
import os
import signal
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deploy
from scripts.deploy_gpu_check import SOURCES, GPUCandidateError, expected_report
from tests import test_deploy_gpu_readiness as fixture_module
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_config import state
from tests.test_deploy_gpu_readiness import readiness as readiness
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.fixture
def candidate(request, monkeypatch):
    original = fixture_module.remember

    def remember(item, generation):
        root = Path(__file__).resolve().parents[1]
        for name in SOURCES:
            generation["manifest"]["source_files"][name] = hashlib.sha256(
                (root / name).read_bytes()
            ).hexdigest()
            generation["manifest"]["gpu"]["source_scopes"]["worker"].append(name)
        original(item, generation)

    monkeypatch.setattr(fixture_module, "remember", remember)
    value = request.getfixturevalue("readiness")
    result = SimpleNamespace(value=value, failure=None, mutate=None)

    def record():
        return state(value.item)["gpu_candidate_checks"][
            value.item.token + ":" + value.item.generation["gpu_worker"]
        ]

    def control(host, generation, *command, **kwargs):
        assert generation == value.item.generation
        assert command[0] == "deployment_gpu_check" and kwargs == {"timeout": 540}
        value.model.change(
            lambda saved: saved.setdefault("candidate_calls", []).append(list(command))
        )
        current = record()
        assert current["phase"] in (
            {"checked", "cleaning"} if command[1] == "cleanup" else {"checking"}
        )
        if result.failure:
            raise RuntimeError(result.failure)
        observed = (
            current["cleanup_observation"] if command[1] == "cleanup" else current["observation"]
        )
        arguments = dict(zip(command[2::2], command[3::2], strict=True))
        assert arguments["--token"] == value.item.token
        assert arguments["--owner-token"] == observed["owner_token"]
        report = expected_report(current, command[1] == "cleanup")
        if result.mutate:
            result.mutate(report)
        return json.dumps(report).encode()

    monkeypatch.setattr(deploy.Deployment, "control", control)

    def run(cleanup=False):
        host = value.item.coordinator()
        with host.locked():
            return host.check_gpu_candidate(value.item.generation, value.release, cleanup=cleanup)

    result.run, result.record = run, record
    result.calls = lambda: value.model.read().get("candidate_calls", [])
    return result


def test_verified_check_is_bound_to_original_startup_and_reobserved(candidate):
    report = candidate.run()
    record = candidate.record()
    assert report["passed"] is True and record["phase"] == "verified"
    assert record["observation"]["started_at"] == candidate.value.started
    assert len(candidate.calls()) == 2
    assert candidate.run() == report
    assert len(candidate.calls()) == 2
    candidate.value.snapshot["device"]["state"] = "quarantined"
    with pytest.raises(GPUCandidateError, match="fresh readiness"):
        candidate.run()
    assert len(candidate.calls()) == 2


def test_failed_command_retains_identity_and_can_resume_with_its_pending_jobs(candidate):
    candidate.failure = "fixed observation timeout"
    with pytest.raises(RuntimeError, match="timeout"):
        candidate.run()
    before = candidate.record()
    assert before["phase"] == "checking" and before["report"] is None
    candidate.value.snapshot.update(pending=True, reservations=True)

    def finish(report):
        candidate.value.snapshot.update(pending=False, reservations=False)

    candidate.failure, candidate.mutate = None, finish
    candidate.run()
    assert candidate.record()["observation"] == before["observation"]
    assert candidate.calls()[0] == candidate.calls()[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", 1),
        ("owner_token", str(uuid.uuid4())),
        ("fingerprint", "a" * 64),
        ("worker_id", "old-worker"),
        ("verdicts", ["accepted"]),
        ("cleanup_passed", True),
        ("protocol", "unknown"),
    ],
)
def test_bad_command_evidence_is_never_saved_as_verified(candidate, field, value):
    candidate.mutate = lambda report: report.update({field: value})
    with pytest.raises(GPUCandidateError, match="incomplete or mismatched"):
        candidate.run()
    assert candidate.record()["phase"] == "checking"
    assert candidate.record()["report"] is None


def test_fresh_readiness_is_required_after_successful_command(candidate):
    candidate.mutate = lambda report: candidate.value.snapshot.update(reservations=True)
    with pytest.raises(GPUCandidateError, match="fresh readiness"):
        candidate.run()
    assert candidate.record()["phase"] == "checking"


def test_lost_cleanup_reply_retries_only_cleanup_after_persisting_verification(
    candidate, monkeypatch
):
    original = deploy.Deployment.control
    lose_reply = True

    def control(host, generation, *command, **kwargs):
        nonlocal lose_reply
        result = original(host, generation, *command, **kwargs)
        if command[1] == "cleanup" and lose_reply:
            lose_reply = False
            raise InterruptedError("lost cleanup reply after commit")
        return result

    monkeypatch.setattr(deploy.Deployment, "control", control)
    with pytest.raises(InterruptedError):
        candidate.run()
    record = candidate.record()
    assert record["phase"] == "checked" and record["report"]["passed"] is True
    assert record["report"]["cleanup_passed"] is False and record["cleanup_report"] is None
    assert candidate.run()["cleanup_passed"] is True
    assert [command[1] for command in candidate.calls()] == ["check", "cleanup", "cleanup"]


def restart(value):
    value.act("stop")
    value.act("stop", "check")
    value.act("check", "check")
    info = value.act("start")
    checked = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00")) + timedelta(
        seconds=1
    )
    value.snapshot["device"].update(owner_token=uuid.uuid4(), checked_at=checked)
    value.snapshot["observed_at"] = checked


def test_restarted_worker_requires_cleanup_and_cannot_reuse_old_verification(candidate):
    candidate.failure = "fixed interruption"
    with pytest.raises(RuntimeError):
        candidate.run()
    original = candidate.record()["observation"]
    restart(candidate.value)
    candidate.failure = None
    with pytest.raises(GPUCandidateError, match="startup changed"):
        candidate.run()
    report = candidate.run(cleanup=True)
    record = candidate.record()
    assert record["phase"] == "cleaned" and report["passed"] is False
    assert record["observation"] == original
    assert record["cleanup_observation"]["owner_token"] != original["owner_token"]
    with pytest.raises(GPUCandidateError, match="cannot authorize"):
        candidate.run()


def test_unfinished_check_blocks_configuration_publication(candidate):
    candidate.failure = "fixed interruption"
    with pytest.raises(RuntimeError):
        candidate.run()
    candidate.value.act("stop")
    with pytest.raises(ValueError, match="candidate verification or cleanup"):
        candidate.value.item.publish(candidate.value.item.generation, enabled=True)


def test_verified_check_allows_stopped_original_configuration_publication(candidate):
    candidate.run()
    candidate.value.act("stop")
    candidate.value.item.publish(candidate.value.item.generation, enabled=True)


@pytest.mark.parametrize("field", ["container", "configuration_sha256"])
def test_cleanup_observation_cannot_switch_identity_in_a_completed_record(candidate, field):
    candidate.failure = "original command interrupted"
    with pytest.raises(RuntimeError):
        candidate.run()
    candidate.failure = None
    candidate.run(cleanup=True)
    value = candidate.value
    host = value.item.coordinator()
    with host.locked():
        key = value.item.token + ":" + value.item.generation["gpu_worker"]
        host.state["gpu_candidate_checks"][key]["cleanup_observation"][field] = "f" * 64
        host.save()
    value.act("stop")
    with pytest.raises(GPUCandidateError, match="Invalid original"):
        value.item.publish(value.item.generation, enabled=True)


@pytest.mark.parametrize("phase", ["checking", "checked", "verified", "cleaning", "cleaned"])
def test_sigkill_continues_original_candidate_phase(candidate, phase):
    if phase in {"cleaning", "cleaned"}:
        candidate.failure = "original command interrupted"
        with pytest.raises(RuntimeError):
            candidate.run()
        candidate.failure = None
    value = candidate.value
    value.model.guard.close()
    value.model.guard = None
    pid = os.fork()
    if pid == 0:
        original = deploy.Deployment.save

        def save(host, *args, **kwargs):
            output = original(host, *args, **kwargs)
            if any(
                row["phase"] == phase for row in host.state.get("gpu_candidate_checks", {}).values()
            ):
                os.kill(os.getpid(), signal.SIGKILL)
            return output

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", save)
                candidate.run(cleanup=phase in {"cleaning", "cleaned"})
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 20
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned candidate child did not reach interruption")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        before = candidate.record()
        candidate.run(cleanup=phase in {"cleaning", "cleaned"})
        assert candidate.record()["observation"] == before["observation"]
        assert candidate.record()["phase"] == (
            "cleaned" if phase in {"cleaning", "cleaned"} else "verified"
        )
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.parametrize("role", ["owner", "web", "worker"])
def test_application_receives_only_the_bound_device_allowlist(
    private_config, tmp_path, monkeypatch, role
):
    _, config = private_config
    config = copy.deepcopy(config)
    config["gpu"] = {"device": "GPU-" + str(uuid.uuid4())}
    host = deploy.Deployment.__new__(deploy.Deployment)
    host.config = config
    host.state = {"gpu_attachment": {"phase": "attached"}}
    monkeypatch.setattr(
        host, "networks", lambda: {"frontend": {"IPAM": {"Config": [{"Gateway": "127.0.0.1"}]}}}
    )
    host._database = SimpleNamespace(
        url=lambda name, role: "postgresql://synthetic@localhost/owned"
    )
    generation = {
        "database": "owned",
        "manifest": {"gpu": {}, "judge": {"bundle_sha256": "a" * 64}},
    }
    environment = host.environment(generation, role)
    assert environment.get("JUDGE_GPU_DEVICES") == (
        config["gpu"]["device"] if role in {"owner", "web"} else None
    )
    assert not any("SOCKET" in key or "RECEIPT" in key for key in environment)
