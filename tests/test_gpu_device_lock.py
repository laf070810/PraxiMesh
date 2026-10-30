"""Real host file-lock checks; no GPU workload or hardware receipt is simulated."""

import os
import subprocess
import sys

import pytest

from judge.gpu_device_lock import DeviceLock, DeviceLockError

DEVICE = "GPU-11111111-2222-3333-4444-555555555555"
CHILD = """
import sys
from judge.gpu_device_lock import DeviceLock, DeviceLockError
try:
    with DeviceLock(sys.argv[1], sys.argv[2]):
        print('owned')
except DeviceLockError:
    raise SystemExit(73)
"""


def test_another_process_cannot_acquire_until_owner_closes(tmp_path):
    directory = tmp_path / "locks"
    with DeviceLock(directory, DEVICE) as first:
        result = subprocess.run(
            [sys.executable, "-c", CHILD, str(directory), DEVICE], capture_output=True, timeout=5
        )
        assert result.returncode == 73
        token = first.owner_token
        inode = (directory / first.filename).stat().st_ino
    assert (directory / first.filename).stat().st_ino == inode
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(directory), DEVICE], capture_output=True, timeout=5
    )
    assert result.returncode == 0 and result.stdout.strip() == b"owned"
    with first:
        assert first.owner_token != token


def test_process_exit_releases_lock_without_unlinking_it(tmp_path):
    directory = tmp_path / "locks"
    code = """
import os,sys
from judge.gpu_device_lock import DeviceLock
guard = DeviceLock(sys.argv[1], sys.argv[2]).acquire()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(directory), DEVICE], check=True, timeout=5)
    with DeviceLock(directory, DEVICE) as guard:
        guard.assert_held()


@pytest.mark.parametrize("defect", ["public_directory", "symlink", "hardlink"])
def test_insecure_or_aliased_lock_paths_are_rejected(tmp_path, defect):
    directory = tmp_path / "locks"
    directory.mkdir(mode=0o755 if defect == "public_directory" else 0o700)
    protected = tmp_path / "untouched"
    protected.write_text("must remain unchanged")
    protected.chmod(0o600)
    lock_path = directory / (DEVICE + ".lock")
    if defect == "symlink":
        lock_path.symlink_to(protected)
    elif defect == "hardlink":
        os.link(protected, lock_path)
    with pytest.raises((DeviceLockError, OSError)):
        DeviceLock(directory, DEVICE).acquire()
    assert protected.read_text() == "must remain unchanged"


def test_replaced_lock_inode_and_closed_guard_cannot_authorize_work(tmp_path):
    with DeviceLock(tmp_path / "locks", DEVICE) as guard:
        path = guard.directory / guard.filename
        path.unlink()
        path.touch(mode=0o600)
        with pytest.raises(DeviceLockError, match="replaced"):
            guard.assert_held()
    with pytest.raises(DeviceLockError, match="not held"):
        guard.assert_held()
