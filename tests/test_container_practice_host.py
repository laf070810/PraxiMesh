"""Real private files, locks and processes; modeled daemon, no NVIDIA access."""

import copy
import json
import os
import select
import socket
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from judge import container_practice_host as module
from judge.container_controller import validate_mounts
from judge.container_journal import ContainerJournal
from judge.container_practice_state import MARKER, OPERATION, LocalPracticeState, inode
from judge.container_processes import host_manifest
from judge.container_service import LocalEngine
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import Engine
from tests.test_gpu_adapter import TARGET


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / "local"
    root.mkdir(mode=0o700)
    endpoint = socket.socket(socket.AF_UNIX)
    endpoint.bind(str(tmp_path / "docker.sock"))
    engine = LocalEngine(tmp_path / "docker.sock")
    fake = Engine()
    monkeypatch.setattr(engine, "run", fake.run)
    monkeypatch.setattr(engine, "verify", lambda: {"ID": fake.identifier})
    observer = {
        "schema": 2,
        "minor": 0,
        "target": copy.deepcopy(TARGET),
        "library": {
            "schema": 2,
            "profile": "nvidia-inventory-v2",
            "driver_version": TARGET["driver"],
            "library": {
                "name": "libnvidia-ml.so." + TARGET["driver"],
                "soname": "libnvidia-ml.so.1",
                "size": 1,
                "sha256": "a" * 64,
            },
            "utility": {"name": "nvidia-smi", "size": 1, "sha256": "b" * 64},
        },
    }
    with DeviceLock(root / "locks", TARGET["uuid"]) as guard:
        with ContainerJournal(root / "journal", guard, engine):
            mounts = {
                "version": 4,
                "operator": [os.getuid(), os.getgid()],
                "root": inode(root.stat()),
                "locks": inode((root / "locks").stat()),
                "journal": inode((root / "journal").stat()),
                "socket": inode(engine.path.stat()),
                "engine": fake.identifier,
                "host_processes": host_manifest(),
                "device_observer": observer,
            }
            private_json(root / "mounts.json", mounts)
    try:
        yield SimpleNamespace(
            root=root, engine=engine, fake=fake, mounts=mounts, device=TARGET["uuid"]
        )
    finally:
        endpoint.close()


def workspace(prepared, **kwargs):
    return module.HostWorkspace(prepared.root, prepared.engine, **kwargs)


def test_explicit_initialization_keeps_original_physical_namespace(prepared):
    original_lock = inode((prepared.root / "locks" / (prepared.device + ".lock")).stat())
    with pytest.raises(SandboxUnavailable, match="initialization"):
        workspace(prepared)
    with workspace(prepared, initialize=True) as host:
        anchor = copy.deepcopy(host.anchor)
        assert host.binding["lock"] == original_lock
        assert not (prepared.root / "journal" / OPERATION).exists()
        with host.physical() as state:
            assert type(state) is LocalPracticeState
            assert state.marker == host.marker
        with DeviceLock(prepared.root / "locks", prepared.device):
            host.check()  # Client supervision does not hold the physical lock.
        with pytest.raises(SandboxUnavailable, match="Another local command"):
            workspace(prepared)
    with workspace(prepared) as reopened:
        assert reopened.anchor == anchor
    assert all(call[0] == "info" for call in prepared.fake.calls)


def test_host_can_reopen_for_recovery_while_original_controller_holds_physical_lock(prepared):
    with workspace(prepared, initialize=True):
        pass
    with DeviceLock(prepared.root / "locks", prepared.device):
        with workspace(prepared) as host:
            host.check()
            with pytest.raises(DeviceLockError, match="Another controller"):
                with host.physical():
                    pytest.fail("Must not acquire another controller's physical lock")


@pytest.mark.parametrize(
    "field", ["engine", "operator", "root", "host_processes", "device_observer"]
)
def test_changed_mounts_are_rejected_without_rewriting_original_state(prepared, field):
    with workspace(prepared, initialize=True) as host:
        value = copy.deepcopy(prepared.mounts)
        value[field] = None
        private_json(prepared.root / "mounts.json", value)
        with pytest.raises(SandboxUnavailable):
            host.check()
    assert not (prepared.root / "journal" / module.RECORD).exists()


@pytest.mark.parametrize(
    "target", ["client", "marker", "physical", "installation", "socket", "journal"]
)
def test_replacement_of_original_identity_is_rejected(prepared, target):
    with workspace(prepared, initialize=True) as host:
        if target == "socket":
            prepared.engine.path.rename(prepared.engine.path.with_suffix(".original"))
            replacement = socket.socket(socket.AF_UNIX)
            replacement.bind(str(prepared.engine.path))
            replacement.close()
        elif target == "journal":
            (prepared.root / "journal").rename(prepared.root / "original-journal")
            (prepared.root / "journal").mkdir(mode=0o700)
        else:
            path = {
                "client": prepared.root / "journal" / module.CLIENT_LOCK,
                "marker": prepared.root / "journal" / MARKER,
                "physical": prepared.root / "locks" / (prepared.device + ".lock"),
                "installation": prepared.root / "journal" / "installation.json",
            }[target]
            raw = path.read_bytes()
            path.rename(path.with_suffix(".original"))
            path.write_bytes(raw if target not in {"marker", "installation"} else b"{}")
            path.chmod(0o600)
        with pytest.raises((SandboxUnavailable, FileNotFoundError)):
            host.check()


def test_durable_record_anchor_and_stale_write_detection(prepared):
    with workspace(prepared, initialize=True) as host:
        value = {"anchor": host.anchor, "phase": "prepared", "nonce": uuid.uuid4().hex}
        host.write_record(value)
        assert host.read(module.RECORD) == value
        path = prepared.root / "journal" / module.RECORD
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list(path.parent.glob(".local-controller-write-*"))
        private_json(path, {**value, "phase": "external-change"})
        with pytest.raises(SandboxUnavailable, match="record changed"):
            host.write_record({**value, "phase": "created"})
        assert json.loads(path.read_text())["phase"] == "external-change"
    with workspace(prepared) as host:
        assert host.record["phase"] == "external-change"


@pytest.mark.parametrize("replace", ["inode", "contents", "missing"])
def test_registered_record_prevents_replacement_client_ownership(prepared, replace):
    with workspace(prepared, initialize=True) as host:
        host.write_record({"anchor": host.anchor, "phase": "prepared"})
    path = prepared.root / "journal" / module.CLIENT_LOCK
    raw = path.read_bytes()
    if replace in {"inode", "missing"}:
        path.rename(path.with_suffix(".original"))
    if replace == "inode":
        path.write_bytes(raw)
        path.chmod(0o600)
    if replace == "contents":
        value = json.loads(raw)
        value["nonce"] = uuid.uuid4().hex
        private_json(path, value)
    with pytest.raises(SandboxUnavailable, match="ownership|different client"):
        workspace(prepared, initialize=True)


@pytest.mark.parametrize("target", ["mounts.json", "locks", "journal", "lock"])
def test_missing_original_state_is_never_recreated(prepared, target):
    path = (
        prepared.root / target
        if target != "lock"
        else prepared.root / "locks" / (prepared.device + ".lock")
    )
    path.rename(path.with_name("original-" + path.name))
    with pytest.raises((SandboxUnavailable, FileNotFoundError)):
        workspace(prepared, initialize=True)
    assert not path.exists()


def test_shared_mount_validator_rejects_boolean_version_and_mismatched_device(prepared):
    assert validate_mounts(prepared.mounts, prepared.device) == prepared.mounts
    with pytest.raises(SandboxUnavailable):
        validate_mounts({**prepared.mounts, "version": True}, prepared.device)
    with pytest.raises(SandboxUnavailable):
        validate_mounts(prepared.mounts, "GPU-" + str(uuid.uuid4()))


def test_marker_binding_cannot_replace_integer_with_boolean(prepared):
    with workspace(prepared, initialize=True) as host:
        marker = copy.deepcopy(host.marker)
        marker["binding"]["installation"]["version"] = True
        private_json(prepared.root / "journal" / MARKER, marker)
    with pytest.raises(SandboxUnavailable, match="different original"):
        workspace(prepared)


def test_killed_client_releases_only_client_flock_and_preserves_record(prepared):
    with workspace(prepared, initialize=True) as host:
        host.write_record({"anchor": host.anchor, "phase": "prepared"})
    script = """
import json, sys, signal
from judge.container_practice_host import HostWorkspace
from judge.container_service import LocalEngine
engine = LocalEngine(sys.argv[2])
engine.verify = lambda: {'ID': sys.argv[3]}
with HostWorkspace(sys.argv[1], engine):
    assert not any(n == 'django' or n.startswith('django.') for n in sys.modules)
    print('locked', flush=True)
    signal.pause()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(prepared.root),
            str(prepared.engine.path),
            prepared.fake.identifier,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == b"locked\n"
        with pytest.raises(SandboxUnavailable, match="Another local command"):
            workspace(prepared)
        process.kill()
        assert process.wait(timeout=10) < 0
        with workspace(prepared) as host:
            assert host.record["phase"] == "prepared"
            with host.physical():
                pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
