"""Identity and boundary checks; actual mapped-file proof has a separate script."""

import hashlib
import json
import os
import time
import uuid

import pytest

from judge import gpu_recovery
from judge.gpu_device_lock import DeviceLock
from judge.gpu_namespace import RUNTIME_OWNER, JobWorkspace
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        service = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
        monkeypatch.setattr(service, "inspect", lambda: None)
        monkeypatch.setattr(service, "populated", lambda: False)
        with JobWorkspace(tmp_path / "jobs", service) as workspace:
            monkeypatch.setattr(
                gpu_recovery,
                "namespace_mapping",
                lambda: {
                    "uid": [[0, 2031, 1], [1, 100000, 65536]],
                    "gid": [[0, 2031, 1], [1, 100000, 65536]],
                },
            )
            gpu_recovery.record_runtime_owner(workspace.path)
            for name in gpu_recovery.STORE_DIRECTORIES:
                (workspace.path / name).mkdir(mode=0o700)
                (workspace.path / name / "owned").write_text("owned")
            yield workspace


def test_same_mapping_recovers_only_runtime_directories_and_can_retry(workspace, tmp_path):
    request = gpu_recovery.cleanup_request(workspace)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("must remain")
    (workspace.path / "data/link").symlink_to(outside, target_is_directory=True)
    expected = list(gpu_recovery.STORE_DIRECTORIES)
    assert gpu_recovery.remove_runtime_files(request, lambda: None) == expected
    assert gpu_recovery.remove_runtime_files(request, lambda: None) == expected
    assert (outside / "keep").read_text() == "must remain"
    assert (workspace.path / RUNTIME_OWNER).is_file()
    workspace.check()
    workspace.remove_after_stop()
    assert not workspace.path.exists()


@pytest.mark.parametrize("defect", ["inode", "device", "identity_sha256", "owner_sha256", "extra"])
def test_changed_request_cannot_remove_owned_store(workspace, defect):
    request = gpu_recovery.cleanup_request(workspace)
    request[defect] = 0 if defect in {"inode", "device"} else "bad"
    with pytest.raises(SandboxUnavailable):
        gpu_recovery.remove_runtime_files(request, lambda: None)
    assert (workspace.path / "data/owned").exists()


@pytest.mark.parametrize("name", ["identity.json", RUNTIME_OWNER])
@pytest.mark.parametrize("defect", ["permissions", "link", "fifo", "hardlink", "contents"])
def test_untrusted_records_stop_recovery_before_deletion(workspace, tmp_path, name, defect):
    request = gpu_recovery.cleanup_request(workspace)
    path = workspace.path / name
    if defect == "permissions":
        path.chmod(0o644)
    elif defect == "hardlink":
        os.link(path, tmp_path / "linked-record")
    elif defect == "contents":
        path.write_text("changed identity")
    else:
        path.unlink()
        if defect == "link":
            path.symlink_to(tmp_path / "unknown")
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises((SandboxUnavailable, OSError)):
        gpu_recovery.remove_runtime_files(request, lambda: None)
    assert (workspace.path / "data/owned").exists()


def test_changed_subordinate_mapping_does_not_start_cleanup(workspace, monkeypatch):
    request = gpu_recovery.cleanup_request(workspace)
    monkeypatch.setattr(
        gpu_recovery, "namespace_mapping", lambda: {"uid": [[0, 2222, 1]], "gid": []}
    )
    with pytest.raises(SandboxUnavailable, match="mapping"):
        gpu_recovery.remove_runtime_files(request, lambda: None)
    assert (workspace.path / "data/owned").exists()


def test_forged_owner_with_correct_transport_hash_still_fails(workspace):
    owner = workspace.path / RUNTIME_OWNER
    record = json.loads(owner.read_text())
    record["inode"] += 1
    owner.write_text(json.dumps(record, sort_keys=True))
    request = gpu_recovery.cleanup_request(workspace)
    assert request["owner_sha256"] == hashlib.sha256(owner.read_bytes()).hexdigest()
    with pytest.raises(SandboxUnavailable, match="identity changed"):
        gpu_recovery.remove_runtime_files(request, lambda: None)


@pytest.mark.parametrize("defect", ["symlink", "mount"])
def test_external_store_roots_are_retained(workspace, tmp_path, monkeypatch, defect):
    request = gpu_recovery.cleanup_request(workspace)
    if defect == "symlink":
        (workspace.path / "run").rename(workspace.path / "saved-run")
        (workspace.path / "run").symlink_to(tmp_path, target_is_directory=True)
    else:
        monkeypatch.setattr(gpu_recovery, "mounted_paths", lambda: [workspace.path / "run"])
    with pytest.raises(SandboxUnavailable):
        gpu_recovery.remove_runtime_files(request, lambda: None)
    assert (workspace.path / "data/owned").exists()


def test_identity_is_preserved_after_interrupted_recovery(workspace):
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SandboxUnavailable("fixed interruption")

    request = gpu_recovery.cleanup_request(workspace)
    with pytest.raises(SandboxUnavailable, match="interruption"):
        gpu_recovery.remove_runtime_files(request, interrupted)
    assert not (workspace.path / "data").exists()
    assert (workspace.path / "run").exists()
    workspace.check()
    gpu_recovery.remove_runtime_files(request, lambda: None)


def test_existing_runtime_identity_cannot_be_overwritten(workspace):
    with pytest.raises(FileExistsError):
        gpu_recovery.record_runtime_owner(workspace.path)


def test_live_task_is_rejected_before_starting_a_recovery_service(workspace, monkeypatch):
    monkeypatch.setattr(workspace.service, "populated", lambda: True)
    with pytest.raises(SandboxUnavailable, match="Stop the owned"):
        gpu_recovery.recover_workspace(
            workspace, deadline=time.monotonic() + 50, on_tick=lambda: None
        )


def test_expired_recovery_is_rejected_before_launch(workspace):
    with pytest.raises(SandboxUnavailable, match="Insufficient"):
        gpu_recovery.recover_workspace(
            workspace, deadline=time.monotonic() - 1, on_tick=lambda: None
        )


def test_host_process_cannot_cleanup_mapped_files():
    with pytest.raises(SandboxUnavailable, match="private controller"):
        gpu_recovery.namespace_mapping()


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True])
def test_invalid_deadline_is_rejected_before_any_service_action(workspace, deadline):
    with pytest.raises(SandboxUnavailable, match="finite"):
        gpu_recovery.recover_workspace(workspace, deadline=deadline, on_tick=lambda: None)
