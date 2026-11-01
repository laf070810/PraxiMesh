"""Real private files/locks/SIGKILL; explicit Docker, DB and frozen-state models."""

import copy
import os
import signal
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from judge.container_deployment_config import GENERATION, TRANSITION
from judge.container_worker_config import read_config
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from scripts import deploy
from scripts.deploy_database import DeploymentDatabaseError
from scripts.deploy_gpu_config import FILES, Publication, context
from scripts.deploy_gpu_retirement import GPURetirementError, validate_record
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
def retirement(quiescence):
    value = quiescence
    value.run()

    def run():
        host = value.coordinator()
        with host.locked():
            return host.retire_gpu_configuration(value.item.generation)

    return SimpleNamespace(
        quiescence=value,
        item=value.item,
        run=run,
        coordinator=value.coordinator,
        record=lambda: state(value.item)["gpu_retirements"][
            value.item.token + ":" + value.item.generation["gpu_worker"]
        ],
    )


def test_retirement_only_removes_original_pair_and_preserves_node_containers_and_history(
    retirement,
):
    value = retirement
    before = state(value.item)
    files = {
        name: (value.item.root / name).read_bytes()
        for name in os.listdir(value.item.root)
        if (value.item.root / name).is_file()
    }
    containers = value.quiescence.model.read()["containers"]
    report = value.run()
    after = state(value.item)
    assert report["phase"] == "retired" and report["from"] == before["gpu_configuration"]["active"]
    assert after["gpu_configuration"] == {"schema": 1, "active": None, "transition": None}
    assert after["current"] == before["current"] and after["operation"] == before["operation"]
    for key in ("gpu_attachment", "gpu_runtime", "gpu_workers", "gpu_checks"):
        assert after[key] == before[key]
    assert value.quiescence.model.read()["containers"] == containers
    assert set(os.listdir(value.item.root)) == set(files) - set(FILES) | {
        name for name in os.listdir(value.item.root) if (value.item.root / name).is_dir()
    }
    for name, contents in files.items():
        if name not in FILES:
            assert (value.item.root / name).read_bytes() == contents
    with pytest.raises(FileNotFoundError):
        read_config(value.item.root)
    assert value.run() == report and value.quiescence.read()["frozen"]


@pytest.mark.parametrize(
    "defect",
    [
        "running",
        "unfrozen",
        "pending",
        "route",
        "exposed",
        "unknown-journal",
        "foreign-file",
        "unknown-temporary",
        "service",
    ],
)
def test_retirement_refuses_unsettled_or_unrelated_state_without_removing_files(
    retirement, monkeypatch, defect
):
    value = retirement
    if defect == "running":

        def live(data):
            info = data["containers"]["/" + value.item.generation["gpu_worker"]]
            info["State"].update(Running=True, Status="running")

        value.quiescence.model.change(live)
    elif defect == "unfrozen":
        monkeypatch.setattr(
            value.item.database,
            "_require_frozen",
            lambda name: (_ for _ in ()).throw(
                DeploymentDatabaseError("modeled database still open")
            ),
        )
    elif defect == "pending":
        value.item.model.drain = False
    elif defect == "route":
        value.item.model.port = 18001
    elif defect == "exposed":
        host = value.coordinator()
        with host.locked():
            host.state["current"] = value.item.generation
            host.state["operation"]["exposure_target"] = value.item.generation["id"]
            host.save()
    elif defect == "unknown-journal":
        (value.item.root / "journal" / "unowned.json").write_text("retain me")
    elif defect == "foreign-file":
        (value.item.root / "worker.json").write_bytes(b"unowned replacement\n")
    elif defect == "unknown-temporary":
        (value.item.root / ".deployment-config-unknown.tmp").write_bytes(b"retain me")
    else:
        value.item.generation["web"] = "unrecorded-web"
    before = {name: (value.item.root / name).read_bytes() for name in FILES}
    with pytest.raises((ValueError, RuntimeError, OSError)):
        value.run()
    assert {name: (value.item.root / name).read_bytes() for name in FILES} == before


def test_lost_publication_ack_reconciles_removed_pair_without_rewriting_history(
    retirement, monkeypatch
):
    value = retirement
    unpublish = Publication.unpublish

    def interrupted(publication, generation):
        unpublish(publication, generation)
        raise RuntimeError("lost retirement acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(Publication, "unpublish", interrupted)
        with pytest.raises(RuntimeError, match="lost retirement"):
            value.run()
    assert value.record()["phase"] == "prepared"
    assert state(value.item)["gpu_configuration"]["active"] is None
    assert not any((value.item.root / name).exists() for name in (*FILES, TRANSITION))
    original = value.record()["from"]
    value.run()
    assert value.record()["phase"] == "retired" and value.record()["from"] == original


def test_pending_retirement_blocks_republication_before_its_file_transition(
    retirement, monkeypatch
):
    value = retirement
    with monkeypatch.context() as patch:
        patch.setattr(
            Publication,
            "unpublish",
            lambda *args: (_ for _ in ()).throw(RuntimeError("before transition")),
        )
        with pytest.raises(RuntimeError, match="before transition"):
            value.run()
    assert state(value.item)["gpu_configuration"]["transition"] is None
    host = value.coordinator()
    with host.locked(), pytest.raises(ValueError, match="retirement before publishing"):
        host.publish_gpu_configuration(value.item.generation, enabled=True)
    assert value.record()["phase"] == "prepared"
    value.run()


@pytest.mark.parametrize("defect", ["origin", "premature-completion"])
def test_unpublish_rechecks_original_retirement_snapshot_before_file_removal(
    retirement, monkeypatch, defect
):
    value = retirement
    unpublish = Publication.unpublish

    def changed(publication, generation):
        record = next(iter(publication.host.state["gpu_retirements"].values()))
        if defect == "origin":
            record["from"]["enabled"] = not record["from"]["enabled"]
        else:
            record["phase"] = "retired"
        return unpublish(publication, generation)

    monkeypatch.setattr(Publication, "unpublish", changed)
    before = {name: (value.item.root / name).read_bytes() for name in FILES}
    with pytest.raises(ValueError, match="original retirement"):
        value.run()
    assert {name: (value.item.root / name).read_bytes() for name in FILES} == before


def test_both_configuration_removals_hold_original_device_and_database_guards(
    retirement, monkeypatch
):
    value = retirement
    unlink = os.unlink
    removed = []

    def checked(name, *args, **kwargs):
        if name in FILES and kwargs.get("dir_fd") is not None:
            assert value.item.model.held == 1
            with (
                pytest.raises(DeviceLockError),
                DeviceLock(value.item.root / "locks", value.item.bound.prepared.device),
            ):
                pytest.fail("Original device lock lost before removing configuration")
            removed.append(name)
        return unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", checked)
    value.run()
    assert removed == list(FILES) and value.item.model.held == 0


def test_lower_level_removal_requires_original_retirement_intent(retirement):
    value = retirement
    host = value.coordinator()
    before = state(value.item)
    with host.locked(), value.item.bound.node().locked() as node:
        with pytest.raises(ValueError, match="exact original durable intent"):
            Publication(host, node, value.item.bound.binding).unpublish(value.item.generation)
    assert state(value.item) == before


def test_next_operation_can_publish_without_taking_over_retired_database_pause(
    retirement, monkeypatch
):
    value = retirement
    original = value.run()
    generation = copy.deepcopy(value.item.generation)
    generation_id = "aaaaaa123456"
    prefix = "px-example-" + generation_id
    generation.update(
        id=generation_id,
        database="px_example_" + generation_id,
        web=prefix + "-web",
        worker=prefix + "-worker",
        gpu_worker=prefix + "-gpu-worker",
    )
    token = str(uuid.uuid4())
    checked = []

    @contextmanager
    def guard(names, operation):
        assert operation == token and names == [generation["database"]]
        checked.append(names)
        yield lambda: None

    monkeypatch.setattr(value.item.database, "gpu_configuration_guard", guard)
    host = value.coordinator()
    with host.locked():
        host.state["operation"] = {
            "id": token,
            "phase": "prepared",
            "previous": value.item.generation,
            "candidate": generation,
            "exposure_target": None,
        }
        host.save()
        metadata = host.publish_gpu_configuration(generation, enabled=True)
    assert checked and read_config(value.item.root, with_generation=True)[1] == metadata
    assert value.record() == original
    assert state(value.item)["gpu_configuration"]["active"]["generation"] == context(generation)


@pytest.mark.parametrize("defect", ["schema", "phase", "binding_sha256", "operation", "generation"])
def test_corrupt_retirement_journal_is_not_a_completion_credential(retirement, defect):
    value = retirement
    row = copy.deepcopy(value.run())
    row[defect] = {
        "schema": True,
        "phase": "unknown",
        "binding_sha256": "bad",
        "operation": "bad",
        "generation": {},
    }[defect]
    with pytest.raises(GPURetirementError):
        validate_record(row)


@pytest.mark.parametrize(
    "point",
    [
        "retirement-intent",
        "publication-intent",
        "marker",
        "worker-removed",
        "generation-removed",
        "published",
        "marker-removed",
        "retired",
    ],
)
def test_sigkill_retirement_continues_ordered_original_removal(retirement, point):
    value = retirement
    pid = os.fork()
    if pid == 0:
        save, marker, unlink = deploy.Deployment.save, Publication.establish_marker, os.unlink

        def kill(name):
            if name == point:
                os.kill(os.getpid(), signal.SIGKILL)

        def saved(host, *args, **kwargs):
            result = save(host, *args, **kwargs)
            row = next(iter(host.state.get("gpu_retirements", {}).values()), None)
            transition = host.state.get("gpu_configuration", {}).get("transition")
            if row:
                if row["phase"] == "retired":
                    kill("retired")
                elif transition is None:
                    kill("retirement-intent")
                elif transition["phase"] == "prepared":
                    kill("publication-intent")
                elif transition["phase"] == "published":
                    kill("published")
            return result

        def marked(publication, *args):
            result = marker(publication, *args)
            kill("marker")
            return result

        def unlinked(name, *args, **kwargs):
            result = unlink(name, *args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                kill(
                    {
                        "worker.json": "worker-removed",
                        GENERATION: "generation-removed",
                        TRANSITION: "marker-removed",
                    }.get(name)
                )
            return result

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(deploy.Deployment, "save", saved)
                patch.setattr(Publication, "establish_marker", marked)
                patch.setattr(os, "unlink", unlinked)
                value.run()
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 25
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned retirement process did not reach the requested kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        value.run()
        assert value.record()["phase"] == "retired"
        assert not any((value.item.root / name).exists() for name in (*FILES, TRANSITION))
        assert value.quiescence.read()["frozen"]
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
