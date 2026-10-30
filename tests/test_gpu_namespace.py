"""Workspace ownership, recovery and credential boundary; no GPU workloads."""

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest

from judge.gpu_device_lock import DeviceLock
from judge.gpu_namespace import JobWorkspace, mounted_paths, prepare_workload_cgroup
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def service(tmp_path, monkeypatch):
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        task = OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())
        monkeypatch.setattr(task, "inspect", lambda: None)
        monkeypatch.setattr(task, "populated", lambda: False)
        yield task


def test_closed_handle_retains_identity_for_replacement_controller(tmp_path, service):
    directory = tmp_path / "jobs"
    with JobWorkspace(directory, service) as workspace:
        path = workspace.path
        (path / "partial-runtime").write_text("owned artifact")
    assert path.is_dir()
    replacement = OwnedJobService(service.guard, service.job_id, service.reservation_token)
    replacement.inspect = lambda: None
    replacement.populated = lambda: False
    with JobWorkspace(directory, replacement, create=False) as recovered:
        assert recovered.identity == workspace.identity
        recovered.remove_after_stop()
    assert not path.exists()


def test_existing_task_cannot_be_overwritten_and_missing_recovery_is_not_created(tmp_path, service):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        with pytest.raises(FileExistsError):
            JobWorkspace(tmp_path / "jobs", service)
        workspace.check()
        workspace.remove_after_stop()
    with pytest.raises(FileNotFoundError):
        JobWorkspace(tmp_path / "jobs", service, create=False)


@pytest.mark.parametrize("defect", ["identity", "permissions", "symlink", "hardlink", "fifo"])
def test_untrusted_identity_is_rejected_without_removing_files(tmp_path, service, defect):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        path = workspace.path / "identity.json"
        if defect == "identity":
            identity = json.loads(path.read_text())
            identity["reservation"] = str(uuid.uuid4())
            path.write_text(json.dumps(identity, sort_keys=True))
        elif defect == "permissions":
            path.chmod(0o644)
        elif defect == "hardlink":
            os.link(path, workspace.path / "extra-identity")
        else:
            path.unlink()
            if defect == "symlink":
                path.symlink_to(tmp_path / "unrelated")
            else:
                os.mkfifo(path, 0o600)
        with pytest.raises((SandboxUnavailable, OSError)):
            workspace.remove_after_stop()
        assert workspace.path.exists()
        with pytest.raises((SandboxUnavailable, OSError)):
            JobWorkspace(tmp_path / "jobs", service, create=False)


@pytest.mark.parametrize("replace", ["directory", "root"])
def test_directory_replacement_is_detected_before_cleanup(tmp_path, service, replace):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        replaced = workspace.path if replace == "directory" else workspace.directory
        replaced.rename(replaced.with_name(replaced.name + "-old"))
        replaced.mkdir(mode=0o700)
        (replaced / "unrelated").write_text("must remain")
        with pytest.raises(SandboxUnavailable, match="replaced"):
            workspace.remove_after_stop()
        assert (replaced / "unrelated").read_text() == "must remain"


@pytest.mark.parametrize("defect", ["public", "symlink"])
def test_workspace_root_must_be_private_and_not_a_symlink(tmp_path, service, defect):
    root = tmp_path / "jobs"
    if defect == "public":
        root.mkdir(mode=0o755)
    else:
        root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((SandboxUnavailable, OSError)):
        JobWorkspace(root, service)
    assert not (
        root / service.unit.removeprefix("praximesh-gpu-").removesuffix(".service")
    ).exists()


@pytest.mark.parametrize(
    ("state", "populated"),
    [({"ActiveState": "active", "MainPID": "12"}, True), (None, True)],
)
def test_live_or_leftover_processes_prevent_cleanup(
    tmp_path, service, monkeypatch, state, populated
):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        monkeypatch.setattr(service, "inspect", lambda: state)
        monkeypatch.setattr(service, "populated", lambda: populated)
        with pytest.raises(SandboxUnavailable, match="Stop"):
            workspace.remove_after_stop()
        workspace.check()


def test_partial_cleanup_preserves_recovery_identity(tmp_path, service, monkeypatch):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        owner = workspace.path / "runtime-owner.json"
        owner.write_text("private recovery identity")

        def denied(*args, **kwargs):
            raise PermissionError("simulated user-mapped runtime directory")

        denied.avoids_symlink_attacks = True
        monkeypatch.setattr("judge.gpu_namespace.shutil.rmtree", denied)
        with pytest.raises(PermissionError):
            workspace.remove_after_stop()
        workspace.check()
        assert owner.read_text() == "private recovery identity"


def test_mounted_content_is_retained_for_runtime_recovery(tmp_path, service, monkeypatch):
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        marker = workspace.path / "keep"
        marker.write_text("keep")
        monkeypatch.setattr("judge.gpu_namespace.mounted_paths", lambda: [workspace.path / "run"])
        with pytest.raises(SandboxUnavailable, match="Unmount"):
            workspace.remove_after_stop()
        assert marker.read_text() == "keep"
        workspace.check()


def test_mountinfo_escaped_paths_are_not_missed(monkeypatch):
    monkeypatch.setattr(
        "judge.gpu_namespace.Path.read_text",
        lambda _self: "121 99 0:25 / /tmp/with\\040space/\\134mount rw - tmpfs tmpfs rw\n",
    )
    assert mounted_paths() == [Path("/tmp/with space/\\mount")]


def test_cleanup_does_not_follow_nested_symlinks(tmp_path, service):
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    with JobWorkspace(tmp_path / "jobs", service) as workspace:
        (workspace.path / "link").symlink_to(outside, target_is_directory=True)
        workspace.remove_after_stop()
    assert (outside / "keep").read_text() == "keep"


def test_launch_is_offline_and_does_not_inherit_worker_credentials(service, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "test-secret-database")
    monkeypatch.setenv("DOCKER_HOST", "test-secret-daemon")
    monkeypatch.setenv("PYTHONPATH", "test-untrusted-module")
    with tempfile.TemporaryDirectory(prefix="px-ns-test-", dir="/tmp") as directory:
        with JobWorkspace(directory, service) as workspace:
            command = workspace.command(["/usr/bin/sleep", "1"])
            assert command[:2] == ["/usr/bin/env", "-i"]
            assert "--net=none" in command and "--cgroupns" in command
            assert not any("test-secret" in value or "test-untrusted" in value for value in command)
            assert command[-2:] == ["/usr/bin/sleep", "1"]
            workspace.remove_after_stop()


def test_overlong_rootlesskit_socket_is_rejected_before_launch(tmp_path, service):
    directory = tmp_path / ("x" * 80)
    with JobWorkspace(directory, service) as workspace:
        with pytest.raises(ValueError, match="shorter"):
            workspace.command(["/usr/bin/sleep", "1"])
        workspace.remove_after_stop()


def test_host_process_cannot_prepare_a_workload_cgroup():
    if Path("/proc/self/cgroup").read_text().strip() == "0::/manager":
        pytest.skip("This rejection check must run outside the controller namespace")
    with pytest.raises(SandboxUnavailable, match="private controller"):
        prepare_workload_cgroup()
