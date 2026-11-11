"""Installer annotations contain fixed categories; the original exit is preserved."""

import io
import sys

import pytest

from scripts import ci_install_diagnostics


def test_failed_repository_is_classified_without_private_values_or_successful_sources():
    labels = ci_install_diagnostics.classifications(
        "Hit:1 https://packages.microsoft.com/ubuntu public\n"
        "Err:2 http://azure.archive.ubuntu.com/ubuntu PRIVATE_SUITE\n"
        "  Connection timed out [IP: PRIVATE_ADDRESS]\n"
        "E: Failed to fetch https://PRIVATE_USER:PRIVATE_PASSWORD@PRIVATE_HOST/PRIVATE_PATH\n"
        "::warning::PRIVATE_INJECTION\n"
    )
    assert labels == ["APT retrieval failed", "network connection timed out", "Ubuntu archive"]


@pytest.mark.parametrize(
    "text,category",
    [
        ("E: Unable to locate package PRIVATE_NAME", "required package unavailable"),
        ("W: GPG error PRIVATE_HOST NO_PUBKEY PRIVATE_KEY", "repository signature rejected"),
        ("E: Could not get lock PRIVATE_PATH", "package manager lock unavailable"),
        ("E: No space left on device PRIVATE_PATH", "disk space exhausted"),
        ("E: PRIVATE_ADDRESS 503 Service Unavailable", "HTTP server error"),
    ],
)
def test_failure_categories_are_static(text, category):
    assert ci_install_diagnostics.classifications(text) == [category]


@pytest.mark.parametrize("exit_code", [0, 23])
def test_original_process_output_and_exit_are_preserved_without_extra_files(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.chdir(tmp_path)
    output = io.BytesIO()
    command = [
        sys.executable,
        "-c",
        "import sys; print('E: Unable to locate package PRIVATE_NAME'); sys.exit(int(sys.argv[1]))",
        str(exit_code),
    ]
    assert ci_install_diagnostics.run_installer(command, output) == exit_code
    data = output.getvalue()
    assert b"E: Unable to locate package PRIVATE_NAME\n" in data
    annotations = [line for line in data.splitlines() if line.startswith(b"::error")]
    assert len(annotations) == (1 if exit_code else 0)
    assert all(b"PRIVATE" not in line for line in annotations)
    assert list(tmp_path.iterdir()) == []


def test_capture_limit_keeps_exit_and_reports_bounded_diagnosis(monkeypatch):
    monkeypatch.setattr(ci_install_diagnostics, "MAX_LOG_BYTES", 16)
    output = io.BytesIO()
    command = [sys.executable, "-c", "import sys; print('PRIVATE' * 10000); sys.exit(37)"]
    assert ci_install_diagnostics.run_installer(command, output) == 37
    data = output.getvalue()
    assert len(data) > 70000
    annotation = data.split(b"::error")[-1]
    assert b"diagnostic capture limit reached" in annotation and b"PRIVATE" not in annotation


def test_missing_installer_does_not_publish_its_path(tmp_path):
    output = io.BytesIO()
    assert ci_install_diagnostics.run_installer([str(tmp_path / "PRIVATE")], output) == 127
    assert b"Installer could not start" in output.getvalue()
    assert b"PRIVATE" not in output.getvalue()


def test_signal_exit_retains_shell_exit_status():
    output = io.BytesIO()
    command = [
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    ]
    assert ci_install_diagnostics.run_installer(command, output) == 143
