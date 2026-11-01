"""Real private files, original locks, fsync and SIGKILL; modeled DB/proxy/daemon.

No GPU, container lifecycle, PostgreSQL lock, or readiness evidence is produced.
"""

import copy
import json
import os
import signal
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from judge.container_deployment_config import GENERATION, TRANSITION, canonical, digest
from judge.container_worker_config import read_config
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from scripts import deploy, deploy_database
from scripts.deploy_gpu_config import Publication
from scripts.deploy_web import fingerprint
from tests.test_container_adapter import configured as configured
from tests.test_container_deployment_config import bound as bound
from tests.test_container_driver import bundle as bundle
from tests.test_container_worker_config import configuration as configuration
from tests.test_deploy import private_config as private_config
from tests.test_deploy_gpu import prepared as prepared


class Crash(BaseException):
    pass


@pytest.fixture
def publication(bound, monkeypatch):
    # These two files were written by this test's metadata-reader fixture only.
    for name in ("worker.json", GENERATION):
        (bound.root / name).unlink()
    generation = copy.deepcopy(bound.generation)
    generation["manifest"]["gpu"]["worker_configuration"] = "generation-pair-v1"
    token = str(uuid.uuid4())
    model = SimpleNamespace(port=None, containers={}, drain=True, held=0, guards=[])
    config = bound.prepared.config

    def docker_output(*args):
        if args[0] == "port":
            return "127.0.0.1:15432\n"
        return json.dumps({"name": "/" + config["database"]["container"], "running": True})

    monkeypatch.setattr(deploy_database, "docker_output", docker_output)
    database = deploy_database.DeploymentDatabase(
        dict(config["database"], instance=config["instance"], site_id=bound.binding["site_id"])
    )

    @contextmanager
    def guard(names, operation):
        assert operation == token
        model.guards.append(list(names))

        def check():
            if not model.drain:
                raise deploy_database.DeploymentDatabaseError("modeled reservation remains")

        check()
        model.held += 1
        try:
            yield check
            check()
        finally:
            model.held -= 1

    monkeypatch.setattr(database, "gpu_configuration_guard", guard)

    def coordinator():
        host = bound.prepared.coordinator()
        host._database = database
        host.proxy.probe = lambda: {"port": model.port}

        def run(*args, **kwargs):
            if args == ("info", "--format", "{{.ID}}"):
                return bound.prepared.engine_id.encode()
            if args == ("container", "ls", "--all", "--quiet", "--no-trunc"):
                return "\n".join(model.containers).encode()
            if args[:2] == ("container", "inspect"):
                return json.dumps([model.containers[key] for key in args[2:]]).encode()
            pytest.fail("Publication attempted external mutation: " + repr(args))

        host.run = run
        return host

    host = coordinator()
    with host.locked():
        host.state["operation"] = {
            "id": token,
            "phase": "prepared",
            "previous": None,
            "candidate": generation,
        }
        host.save()

    def publish(target=None, *, enabled=False):
        host = coordinator()
        with host.locked():
            return host.publish_gpu_configuration(target or generation, enabled=enabled)

    return SimpleNamespace(
        bound=bound,
        root=bound.root,
        generation=generation,
        token=token,
        model=model,
        database=database,
        coordinator=coordinator,
        publish=publish,
    )


def state(item):
    return json.loads((item.bound.prepared.directory / "deployment.json").read_text())


def remember(item, generation):
    host = item.coordinator()
    with host.locked():
        host.state["operation"]["previous"] = host.state["operation"]["candidate"]
        host.state["operation"]["candidate"] = generation
        host.save()


def next_generation(item):
    value = copy.deepcopy(item.generation)
    value.update(
        id="123456abcdef",
        database="px_example_123456abcdef",
        gpu_worker="px-example-123456abcdef-gpu-worker",
        release_sha256="a" * 64,
    )
    value["manifest"]["images"]["gpu_worker"] = "sha256:" + "b" * 64
    remember(item, value)
    return value


def contents(item):
    return {path.name: path.read_bytes() for path in item.root.iterdir() if path.is_file()}


def fail_at(monkeypatch, stage, crash):
    """Interrupt after actual writes; no production fault-injection switch."""
    save, marker, replace = (
        deploy.Deployment.save,
        Publication.establish_marker,
        Publication.replace,
    )
    opening, syncing, unlinking = os.open, os.fsync, os.unlink

    def saved(host, *args, **kwargs):
        result = save(host, *args, **kwargs)
        config = host.state.get("gpu_configuration")
        if config is not None:
            transition = config["transition"]
            point = "confirmed" if transition is None else transition["phase"]
            if point == stage:
                crash()
        return result

    def marked(self, *args):
        result = marker(self, *args)
        if stage == "marker":
            crash()
        return result

    def replaced(self, intent, name, *args):
        result = replace(self, intent, name, *args)
        if stage == name:
            crash()
        return result

    def opened(path, flags, *args, **kwargs):
        fd = opening(path, flags, *args, **kwargs)
        if stage == "empty-marker" and path == TRANSITION and flags & os.O_CREAT:
            os.close(fd)
            crash()
        return fd

    def synced(fd):
        result = syncing(fd)
        try:
            name = os.readlink(f"/proc/self/fd/{fd}").rsplit("/", 1)[-1]
        except OSError:
            name = ""
        if (
            stage == "config-temporary"
            and name.startswith(".deployment-config-")
            and name.endswith("-worker.json.tmp")
        ):
            crash()
        return result

    def unlinked(path, *args, **kwargs):
        result = unlinking(path, *args, **kwargs)
        if stage == "marker-removed" and path == TRANSITION:
            crash()
        return result

    monkeypatch.setattr(deploy.Deployment, "save", saved)
    monkeypatch.setattr(Publication, "establish_marker", marked)
    monkeypatch.setattr(Publication, "replace", replaced)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "unlink", unlinked)


def test_initial_then_cross_database_publication_is_private_and_idempotent(publication):
    item = publication
    record = item.publish()
    config, actual = read_config(item.root, with_generation=True)
    assert actual == record and config["enabled"] is False
    assert record["database"] == item.generation["database"]
    assert item.database.config["worker_password"] not in json.dumps(state(item))
    assert set(path.name for path in item.root.iterdir()) == {
        "locks",
        "journal",
        "mounts.json",
        "deployment-owner.json",
        "worker.json",
        GENERATION,
    }
    before = [(item.root / name).stat().st_mtime_ns for name in ("worker.json", GENERATION)]
    assert item.publish() == record
    assert before == [(item.root / name).stat().st_mtime_ns for name in ("worker.json", GENERATION)]
    target = next_generation(item)
    item.publish(target, enabled=True)
    config, actual = read_config(item.root, with_generation=True)
    assert actual["generation"] == target["id"] and config["enabled"] is True
    assert item.generation["database"] not in config["database"]["url"]
    assert set(item.model.guards[-1]) == {item.generation["database"], target["database"]}
    assert state(item)["gpu_configuration"]["transition"] is None


STAGES = [
    "prepared",
    "empty-marker",
    "marker",
    "config-temporary",
    "worker.json",
    GENERATION,
    "published",
    "marker-removed",
    "confirmed",
]


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("upgrade", [False, True])
def test_real_sigkill_recovers_only_the_original_target_and_releases_original_locks(
    publication, stage, upgrade
):
    item = publication
    if upgrade:
        item.publish()
        target = next_generation(item)
    else:
        target = item.generation
    device_lock = item.root / "locks" / (item.bound.prepared.device + ".lock")
    identity = (device_lock.stat().st_dev, device_lock.stat().st_ino)
    pid = os.fork()
    if pid == 0:
        try:
            with pytest.MonkeyPatch.context() as patch:
                fail_at(patch, stage, lambda: os.kill(os.getpid(), signal.SIGKILL))
                item.publish(target, enabled=True)
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 15
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned publication child did not reach the interruption")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        pending = (item.root / TRANSITION).exists()
        if pending:
            with pytest.raises((ValueError, FileNotFoundError)):
                read_config(item.root)
        elif stage == "prepared" and upgrade:
            assert (
                read_config(item.root, with_generation=True)[1]["generation"]
                == item.generation["id"]
            )
        item.publish(target, enabled=True)
        config, record = read_config(item.root, with_generation=True)
        assert config["enabled"] is True and record["generation"] == target["id"]
        assert state(item)["gpu_configuration"]["transition"] is None
        assert not (item.root / TRANSITION).exists()
        assert not list(item.root.glob(".deployment-config-*"))
        assert (device_lock.stat().st_dev, device_lock.stat().st_ino) == identity
        with DeviceLock(item.root / "locks", item.bound.prepared.device):
            pass
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def interrupted(item, stage, monkeypatch):
    def crash():
        raise Crash()

    with monkeypatch.context() as patch:
        fail_at(patch, stage, crash)
        with pytest.raises(Crash):
            item.publish()


@pytest.mark.parametrize(
    "defect",
    [
        "target",
        "token",
        "credential",
        "config",
        "metadata",
        "marker",
        "missing-marker",
        "reverse",
        "temporary",
    ],
)
def test_interrupted_publication_refuses_retargeting_or_unknown_files(
    publication, monkeypatch, defect
):
    item = publication
    interrupted(item, "worker.json", monkeypatch)
    target = item.generation
    if defect == "target":
        target = next_generation(item)
    elif defect == "token":
        host = item.coordinator()
        with host.locked():
            host.state["operation"]["id"] = str(uuid.uuid4())
            host.save()
    elif defect == "credential":
        item.database.config["worker_password"] += "changed"
    elif defect in {"config", "metadata", "marker"}:
        filename = {"config": "worker.json", "metadata": GENERATION, "marker": TRANSITION}[defect]
        (item.root / filename).write_bytes(b"unknown")
        (item.root / filename).chmod(0o600)
    elif defect == "missing-marker":
        (item.root / TRANSITION).unlink()
    elif defect == "reverse":
        record = state(item)["gpu_configuration"]["transition"]["intent"]["to"]["metadata"]
        (item.root / GENERATION).write_bytes(canonical(record) + b"\n")
        (item.root / GENERATION).chmod(0o600)
        (item.root / "worker.json").unlink()
    else:
        (item.root / ".deployment-config-unknown.tmp").write_bytes(b"untouched")
    before = contents(item)
    with pytest.raises((ValueError, deploy_database.DeploymentDatabaseError)):
        item.publish(target)
    assert contents(item) == before


@pytest.mark.parametrize(
    "defect",
    [
        "maintenance",
        "drain",
        "active-task",
        "unknown-journal",
        "untracked-config",
        "orphan-marker",
        "legacy-release",
        "missing-record",
    ],
)
def test_prerequisites_refuse_without_writing_configuration(publication, defect):
    item = publication
    if defect == "maintenance":
        item.model.port = 8080
    elif defect == "drain":
        item.model.drain = False
    elif defect in {"active-task", "unknown-journal"}:
        name = (
            item.bound.prepared.device + ".active.json"
            if defect == "active-task"
            else ".write-unknown"
        )
        (item.root / "journal" / name).write_text("must remain")
    elif defect == "untracked-config":
        (item.root / "worker.json").write_text("{}")
        (item.root / "worker.json").chmod(0o600)
    elif defect == "orphan-marker":
        (item.root / TRANSITION).touch(mode=0o600)
    elif defect == "legacy-release":
        del item.generation["manifest"]["gpu"]["worker_configuration"]
        remember(item, item.generation)
    else:
        item.generation["release_sha256"] = "f" * 64
    before = contents(item)
    with pytest.raises((ValueError, deploy_database.DeploymentDatabaseError)):
        item.publish()
    assert contents(item) == before
    assert "gpu_configuration" not in state(item)


def test_publication_needs_both_original_locks(publication):
    item = publication
    with pytest.raises(deploy.DeploymentError, match="original deployment lock"):
        item.coordinator().publish_gpu_configuration(item.generation)
    with DeviceLock(item.root / "locks", item.bound.prepared.device):
        with pytest.raises(DeviceLockError):
            item.publish()
    assert not (item.root / "worker.json").exists()


def test_original_node_change_while_lock_is_held_is_not_accepted(publication, monkeypatch):
    original = Publication.establish_marker

    def mutate(self, expected):
        original(self, expected)
        with (self.node.root / "mounts.json").open("ab") as stream:
            stream.write(b"\n")

    monkeypatch.setattr(Publication, "establish_marker", mutate)
    with pytest.raises(ValueError, match="mount or installation changed"):
        publication.publish()
    assert (publication.root / TRANSITION).exists()
    assert not (publication.root / "worker.json").exists()


def test_drain_loss_after_config_write_leaves_reader_blocked(publication, monkeypatch):
    original = Publication.replace

    def change(self, intent, name, *args):
        original(self, intent, name, *args)
        if name == "worker.json":
            publication.model.drain = False

    monkeypatch.setattr(Publication, "replace", change)
    with pytest.raises(deploy_database.DeploymentDatabaseError):
        publication.publish()
    assert (publication.root / TRANSITION).exists()
    with pytest.raises(ValueError, match="unfinished"):
        read_config(publication.root)
    assert state(publication)["gpu_configuration"]["active"] is None


def runtime(item):
    generation = item.generation
    info = {
        "Id": "a" * 64,
        "Name": "/" + generation["gpu_worker"],
        "Image": generation["manifest"]["images"]["gpu_worker"],
        "Config": {
            "Labels": {
                "org.praximesh.site": item.bound.binding["site_id"],
                "org.praximesh.generation": generation["id"],
                "org.praximesh.role": "gpu_worker",
                "org.praximesh.gpu-binding": digest(item.bound.binding),
            }
        },
        "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
        "Mounts": [{"Source": str(item.root), "Destination": "/state"}],
        "State": {
            "Running": False,
            "Paused": False,
            "Restarting": False,
            "Pid": 0,
            "Status": "exited",
        },
    }
    host = item.coordinator()
    with host.locked():
        host.state["gpu_workers"] = {
            generation["id"]: {
                "id": info["Id"],
                "name": generation["gpu_worker"],
                "image": info["Image"],
                "binding_sha256": digest(item.bound.binding),
                "runtime_sha256": fingerprint(info),
            }
        }
        host.save()
    item.model.containers[info["Id"]] = info
    return info


@pytest.mark.parametrize(
    "defect",
    [
        "running",
        "restart",
        "id",
        "image",
        "binding",
        "unrecorded",
        "unlabelled-mount",
        "ancestor-mount",
    ],
)
def test_original_container_must_be_stopped_and_unknown_mounts_are_not_adopted(publication, defect):
    item = publication
    info = runtime(item)
    if defect == "running":
        info["State"].update(Running=True, Pid=123, Status="running")
    elif defect == "restart":
        info["HostConfig"]["RestartPolicy"]["Name"] = "always"
    elif defect == "id":
        item.model.containers.clear()
        info["Id"] = "b" * 64
        item.model.containers[info["Id"]] = info
    elif defect == "image":
        info["Image"] = "sha256:" + "f" * 64
    elif defect == "binding":
        info["Config"]["Labels"]["org.praximesh.gpu-binding"] = "f" * 64
    else:
        unknown = copy.deepcopy(info)
        unknown.update(Id="b" * 64, Name="/unknown")
        if defect != "unrecorded":
            unknown["Config"]["Labels"] = {}
        if defect == "ancestor-mount":
            unknown["Mounts"][0]["Source"] = str(item.root.parent)
        item.model.containers[unknown["Id"]] = unknown
    before = copy.deepcopy(item.model.containers)
    with pytest.raises(ValueError):
        item.publish()
    assert item.model.containers == before
    assert not (item.root / "worker.json").exists()


def test_known_stopped_original_and_confirmed_absent_runtime_allow_publication(publication):
    runtime(publication)
    publication.publish()
    publication.model.containers.clear()
    publication.publish(next_generation(publication))


def test_recovery_resyncs_complete_marker_before_touching_configuration(publication, monkeypatch):
    interrupted(publication, "marker", monkeypatch)
    synced = set()
    original_sync, original_replace = os.fsync, Publication.replace

    def sync(fd):
        synced.add(os.readlink(f"/proc/self/fd/{fd}").rsplit("/", 1)[-1])
        return original_sync(fd)

    def replace(self, *args):
        assert TRANSITION in synced, "Complete marker read after a crash must be fsynced again"
        return original_replace(self, *args)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Publication, "replace", replace)
    publication.publish()


@pytest.mark.parametrize("kind", ["marker", "config-prefix", "record-prefix", "unknown-prefix"])
def test_only_original_partial_write_prefixes_can_be_recovered(publication, monkeypatch, kind):
    interrupted(publication, "marker", monkeypatch)
    intent = state(publication)["gpu_configuration"]["transition"]["intent"]
    if kind == "marker":
        path = publication.root / TRANSITION
        path.write_bytes(path.read_bytes()[:10])
    else:
        name = GENERATION if kind == "record-prefix" else "worker.json"
        path = publication.root / (".deployment-config-" + intent["id"] + "-" + name + ".tmp")
        path.write_bytes(b"unknown" if kind == "unknown-prefix" else b'{"')
        path.chmod(0o600)
    if kind == "unknown-prefix":
        before = path.read_bytes()
        with pytest.raises(ValueError, match="temporary differs"):
            publication.publish()
        assert path.read_bytes() == before and (publication.root / TRANSITION).exists()
    else:
        publication.publish()
        assert (
            read_config(publication.root, with_generation=True)[1]["generation"]
            == publication.generation["id"]
        )
        assert not path.exists()


@pytest.mark.parametrize("defect", [None, "phase", "installation", "nonce"])
def test_only_valid_original_removed_task_archives_are_compatible(publication, defect):
    from judge.container_journal import ContainerJournal
    from tests.test_container_journal import intent as synthetic_intent

    # A synthetic archive tests parsing; no task or removal is executed.
    with publication.bound.node().locked() as node:
        with ContainerJournal(node.root / "journal", node.guard, node.engine) as journal:
            synthetic_intent(journal)
            record = journal.load()
            record["phase"] = "removed"
            name = node.config["device"] + "." + record["arguments"][2] + ".done.json"
            if defect == "phase":
                record["phase"] = "intent"
            elif defect == "installation":
                record["installation"] = "f" * 32
            elif defect == "nonce":
                name = node.config["device"] + "." + "f" * 32 + ".done.json"
            journal.write(name, record)
            (node.root / "journal" / journal.filename).unlink()
    archive = publication.root / "journal" / name
    before = archive.read_bytes()
    if defect is None:
        publication.publish()
    else:
        from judge.sandbox import SandboxUnavailable

        with pytest.raises((ValueError, SandboxUnavailable)):
            publication.publish()
    assert archive.read_bytes() == before
