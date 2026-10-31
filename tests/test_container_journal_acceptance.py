"""The standalone upgrade check must import only its staged source snapshot."""

import os
import subprocess
import sys

from scripts.container_journal_acceptance import stage_upgrade


def test_upgrade_snapshot_imports_without_original_repository(tmp_path):
    directory = tmp_path / "upgraded"
    stage_upgrade(directory)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(directory / "scripts/container_journal_acceptance.py"),
            "--help",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath, "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--runtime-archive" in result.stdout
