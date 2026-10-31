"""Control mount refusal tests; actual namespace/flock claims use CPU acceptance."""

import hashlib
import json
import os
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge import container_controller as module
from judge.container_controller import ContainerController, identity, mapped_zero
from judge.container_service import LocalEngine
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from judge.sandbox import SandboxUnavailable
from scripts.container_controller_acceptance import stage_cli
from tests.test_container_journal import Engine


@pytest.mark.parametrize(
    "mapping",
    [
        "",
        "0 0 4294967295",
        "0 2031 65536",
        "1 2031 1",
        "0 2031",
        "0 x 1",
        "0 2031 1\n0 2032 1",
        "0 2031 1\n1 -5 10",
    ],
)
def test_unknown_or_host_root_mapping_is_rejected(mapping):
    with pytest.raises(SandboxUnavailable, match="mapping"):
        mapped_zero(mapping)


def test_rootless_mapping_does_not_confuse_subordinate_users_with_operator():
    assert mapped_zero("0 2031 1\n1 624288 65536") == 2031


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    for name in ("locks", "journal"):
        (root / name).mkdir(mode=0o700)
    endpoint = socket.socket(socket.AF_UNIX)
    endpoint.bind(str(tmp_path / "docker.sock"))
    engine = LocalEngine(tmp_path / "docker.sock")
    fake = Engine()
    monkeypatch.setattr(engine, "run", fake.run)
    monkeypatch.setattr(engine, "verify", lambda: None)
    monkeypatch.setattr(module, "STATE", root)
    monkeypatch.setattr(module, "SOCKET", engine.path)
    monkeypatch.setattr(module, "LocalEngine", lambda _path: engine)
    monkeypatch.setattr(module, "environment", lambda operator: {"mapped_operator": operator})
    value = {
        "version": 1,
        "operator": [2031, 2025],
        "root": identity(root),
        "locks": identity(root / "locks"),
        "journal": identity(root / "journal"),
        "socket": identity(engine.path),
        "engine": fake.identifier,
    }
    path = root / "mounts.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    try:
        yield SimpleNamespace(
            root=root,
            engine=engine,
            fake=fake,
            value=value,
            device="GPU-" + str(uuid.uuid4()),
            path=path,
        )
    finally:
        endpoint.close()


def test_controller_uses_original_device_lock_and_installation(mounts):
    with ContainerController(mounts.device) as control:
        original = control.journal.identity
        assert original["lock_namespace"] == mounts.value["locks"]
        with pytest.raises(DeviceLockError, match="Another controller"):
            DeviceLock(mounts.root / "locks", mounts.device).acquire()
        with pytest.raises(DeviceLockError, match="Another controller"):
            ContainerController(mounts.device)
    with ContainerController(mounts.device) as control:
        assert control.journal.identity == original


@pytest.mark.parametrize("key", ["root", "locks", "journal", "socket"])
def test_wrong_mount_inode_fails_before_engine_or_lock_acquisition(mounts, key):
    mounts.value[key][1] += 1
    mounts.path.write_text(json.dumps(mounts.value))
    with pytest.raises(SandboxUnavailable, match="mount changed"):
        ContainerController(mounts.device)
    assert mounts.fake.calls == []
    assert list((mounts.root / "journal").iterdir()) == []
    assert list((mounts.root / "locks").iterdir()) == []


@pytest.mark.parametrize(
    "change", ["engine", "version", "boolean-inode", "extra", "permissions", "symlink"]
)
def test_invalid_manifest_fails_closed(mounts, change):
    if change == "engine":
        mounts.value["engine"] = "not-a-daemon-id"
    elif change == "version":
        mounts.value["version"] = True
    elif change == "boolean-inode":
        mounts.value["locks"][1] = True
    elif change == "extra":
        mounts.value["command"] = "/bin/sh"
    elif change == "permissions":
        mounts.path.chmod(0o644)
    else:
        other = mounts.path.with_name("original.json")
        mounts.path.rename(other)
        mounts.path.symlink_to(other)
    mounts.path.write_text(json.dumps(mounts.value))
    with pytest.raises((SandboxUnavailable, OSError)):
        ContainerController(mounts.device)
    assert mounts.fake.calls == []


def test_different_daemon_releases_lock_without_overwriting_old_journal(mounts):
    with ContainerController(mounts.device) as control:
        original = control.journal.identity
    mounts.fake.identifier = str(uuid.uuid4())
    with pytest.raises(SandboxUnavailable, match="different engine"):
        ContainerController(mounts.device)
    with DeviceLock(mounts.root / "locks", mounts.device):
        assert json.loads((mounts.root / "journal" / "installation.json").read_text()) == original


@pytest.mark.parametrize("change", ["manifest", "locks", "journal", "socket"])
def test_live_mount_changes_stop_control_without_touching_records(mounts, change):
    with ContainerController(mounts.device) as control:
        previous = (mounts.root / "journal" / "installation.json").read_bytes()
        if change == "manifest":
            mounts.value["operator"][0] += 1
            mounts.path.write_text(json.dumps(mounts.value))
        else:
            path = mounts.engine.path if change == "socket" else mounts.root / change
            path.rename(path.with_name("original-" + path.name))
            if change == "socket":
                path.touch(mode=0o600)
            else:
                path.mkdir(mode=0o700)
        with pytest.raises(SandboxUnavailable, match="changed"):
            control.check()
        original_path = mounts.root / ("original-journal" if change == "journal" else "journal")
        assert (original_path / "installation.json").read_bytes() == previous


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    cli = tmp_path / "docker"
    cli.write_bytes(b"fixed-cli")
    monkeypatch.setattr(module, "CLI", cli)
    monkeypatch.setattr(module, "CLI_SHA256", hashlib.sha256(cli.read_bytes()).hexdigest())
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "controller_digest", lambda: "contract")
    (tmp_path / "controller-contract").write_text("contract")
    status = {key: "0" for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")}
    status["NoNewPrivs"] = "1"
    files = {
        "uid_map": "0 2031 1\n1 500000 65536",
        "gid_map": "0 2025 1",
        "cgroup": "0::/",
        "cpu.max": "100000 100000",
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "128",
    }
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/status":
            return "\n".join(key + ": " + value for key, value in status.items())
        if str(path.parent) in {"/proc/self", "/sys/fs/cgroup"} and path.name in files:
            return files[path.name]
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "getgid", lambda: 0)
    monkeypatch.setattr(module.os, "statvfs", lambda _path: SimpleNamespace(f_flag=os.ST_RDONLY))
    return files, status, cli


@pytest.mark.parametrize(
    "change", ["uid", "gid", "capabilities", "privileges", "cgroup", "writable", "cli", "source"]
)
def test_wrong_operator_or_container_contract_is_rejected(kernel, monkeypatch, change):
    files, status, cli = kernel
    assert module.environment([2031, 2025])["uid"] == 0
    if change == "uid":
        files["uid_map"] = "0 2032 1"
    elif change == "gid":
        files["gid_map"] = "0 2026 1"
    elif change == "capabilities":
        status["CapBnd"] = "1"
    elif change == "privileges":
        status["NoNewPrivs"] = "0"
    elif change == "cgroup":
        files["cgroup"] = "0::/user.slice"
    elif change == "writable":
        monkeypatch.setattr(module.os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    elif change == "cli":
        cli.write_bytes(b"changed")
    else:
        (cli.parent / "controller-contract").write_text("old-source")
    with pytest.raises(SandboxUnavailable):
        module.environment([2031, 2025])


def test_unverified_cli_archive_is_not_extracted_or_executed(tmp_path):
    archive = tmp_path / "untrusted.tgz"
    archive.write_bytes(b"wrong archive")
    with pytest.raises(ValueError, match="digest mismatch"):
        stage_cli(archive, tmp_path / "docker")
    assert not (tmp_path / "docker").exists()


@pytest.mark.parametrize("key", ["cpu.max", "memory.max", "memory.swap.max", "pids.max"])
def test_missing_actual_control_limit_cannot_be_replaced_by_create_arguments(kernel, key):
    files, _status, _cli = kernel
    files[key] = "max"
    with pytest.raises(SandboxUnavailable, match="limits"):
        module.environment([2031, 2025])
