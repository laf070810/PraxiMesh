"""Compose real original-file lifecycle components; daemon/DB/GPU remain models."""

import copy

import pytest

from scripts import deploy_gpu_release as module
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared
from tests.test_deploy_gpu_candidate import preparation as preparation
from tests.test_deploy_gpu_check import candidate as candidate
from tests.test_deploy_gpu_config import publication as publication
from tests.test_deploy_gpu_exposure import exposure as exposure
from tests.test_deploy_gpu_readiness import readiness as readiness
from tests.test_deploy_gpu_worker import lifecycle as lifecycle


@pytest.mark.parametrize("exit_during_exposure", [False, True])
def test_whole_recovery_composes_original_worker_preparation_and_exposure(
    exposure, monkeypatch, exit_during_exposure
):
    value = exposure
    value.run()
    generation = value.item.generation
    before = copy.deepcopy(value.record())
    writes = value.saved()["writes"]
    host = value.coordinator()
    release = value.preparation.value.release
    # Match the existing component fixture's explicit complete-image verifier
    # model. Source tar bytes and literal protocol are still actually checked.
    monkeypatch.setattr(module, "verify", lambda path, sha: copy.deepcopy(generation["manifest"]))
    monkeypatch.setattr(value.item.database, "_require_operation_lock", host.require_locked)

    def thaw(name):
        assert name == generation["database"]

    monkeypatch.setattr(value.item.database, "thaw", thaw)
    host.cleanup_controls = lambda: None
    host.proxy.maintenance = lambda: value.change(port=None)
    control = host.control

    def command(gen, *arguments, **kwargs):
        if arguments[:2] == ("judge_admission", "pause"):
            assert gen == generation and arguments[2:] == ("--token", value.item.token)
            value.change(pause=True)
            return b"{}"
        if arguments[0] == "deployment_gpu_check":
            # Candidate fixture owns the fixed queue/score model; exposure's
            # instance-level control only models admission status and resume.
            return type(host).control(host, gen, *arguments, **kwargs)
        return control(gen, *arguments, **kwargs)

    host.control = command
    with host.locked():
        host.state["operation"].update(previous=None, candidate=copy.deepcopy(generation))
        host.state["gpu_releases"] = {
            value.item.token: {
                "schema": 1,
                "operation": value.item.token,
                "previous": None,
                "candidate": copy.deepcopy(generation),
                "archives": {generation["id"]: str(release)},
                "initial_content": False,
                "phase": "active",
                "recoveries": [],
            }
        }
        host.save()
    if exit_during_exposure:
        value.failure = "proxy"
        with host.locked(), pytest.raises(RuntimeError, match="lost proxy"):
            host.recover_coordinated()
        value.failure = None
        value.change(port=None, pause=True)
        value.preparation.value.act("stop")
    with host.locked():
        host.recover_coordinated()
    assert host.state["current"] == generation
    assert host.state["operation"]["phase"] == "active"
    assert value.saved()["writes"][: len(writes)] == writes
    assert before in host.state["gpu_exposures"].values()
    assert value.record()["round_id"] != before["round_id"]
    assert value.record()["observation"]["started_at"] != before["observation"]["started_at"]
    assert all(
        row["phase"] == "finished"
        for row in host.state["gpu_releases"][value.item.token]["recoveries"]
    )
