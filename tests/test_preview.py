"""Preview bootstrap filesystem/recovery checks; real Compose is checked separately."""

import os
import sqlite3
import stat
import subprocess

import pytest

from core import preview


@pytest.fixture
def preview_env(monkeypatch):
    monkeypatch.setenv("PRAXIMESH_MODE", "local")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("JUDGE_ENABLED", "0")
    monkeypatch.setenv("COACHING_ENABLED", "0")
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)


def test_failed_initial_import_publishes_no_database_and_can_retry(
    tmp_path, preview_env, monkeypatch
):
    failed = True

    def manage(environment, *arguments):
        with sqlite3.connect(environment["SQLITE_PATH"]) as connection:
            if arguments[0] == "migrate":
                connection.execute("CREATE TABLE initialized (value TEXT)")
            elif arguments[0] == "project_import" and failed:
                raise subprocess.CalledProcessError(1, ["project_import"])
            elif arguments[0] == "challenge_import":
                connection.execute("INSERT INTO initialized VALUES ('complete')")

    monkeypatch.setattr(preview, "manage", manage)
    with pytest.raises(subprocess.CalledProcessError):
        preview.prepare(tmp_path)
    assert not (tmp_path / "praximesh.sqlite3").exists()
    assert not list(tmp_path.glob(".preview-bootstrap-*"))
    first_key = (tmp_path / "preview-secret.key").read_bytes()
    failed = False
    environment = preview.prepare(tmp_path)
    with sqlite3.connect(environment["SQLITE_PATH"]) as connection:
        assert connection.execute("SELECT value FROM initialized").fetchall() == [("complete",)]
    assert (tmp_path / "preview-secret.key").read_bytes() == first_key
    assert stat.S_IMODE((tmp_path / "praximesh.sqlite3").stat().st_mode) == 0o600


def test_existing_database_never_reimports_content(tmp_path, preview_env, monkeypatch, capsys):
    database = tmp_path / "praximesh.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE private_data (value TEXT)")
        connection.execute("INSERT INTO private_data VALUES ('preserve me')")
    before = database.read_bytes()
    calls = []
    monkeypatch.setattr(preview, "manage", lambda environment, *args: calls.append(args))
    first = preview.prepare(tmp_path)
    second = preview.prepare(tmp_path)
    assert first["APP_SECRET_KEY"] == second["APP_SECRET_KEY"]
    assert len(first["APP_SECRET_KEY"]) >= 50
    assert first["APP_SECRET_KEY"] not in capsys.readouterr().out
    assert database.read_bytes() == before
    assert calls == [("migrate", "--noinput"), ("migrate", "--noinput")]
    assert stat.S_IMODE((tmp_path / "preview-secret.key").stat().st_mode) == 0o600


@pytest.mark.parametrize("contents", [b"", b"short", b"a" * 50 + b"\n", b"\xff" * 60])
def test_invalid_stored_secret_is_not_silently_replaced(tmp_path, contents):
    key = tmp_path / "preview-secret.key"
    key.write_bytes(contents)
    key.chmod(0o600)
    with pytest.raises((preview.PreviewError, UnicodeError)):
        preview.secret_key(tmp_path, "")
    assert key.read_bytes() == contents


def test_explicit_instance_secret_remains_supported(tmp_path):
    provided = "existing-private-instance-key-" * 3
    assert preview.secret_key(tmp_path, provided) == provided
    assert not (tmp_path / "preview-secret.key").exists()
    with pytest.raises(preview.PreviewError):
        preview.secret_key(tmp_path, "short")


def test_world_readable_secret_is_rejected(tmp_path):
    path = tmp_path / "preview-secret.key"
    path.write_text("a" * 80)
    path.chmod(0o644)
    with pytest.raises(preview.PreviewError, match="private"):
        preview.secret_key(tmp_path, "")


@pytest.mark.parametrize("name", ["preview-secret.key", "praximesh.sqlite3", "preview.lock"])
@pytest.mark.parametrize("exists", [False, True], ids=["dangling", "existing"])
def test_preview_refuses_symlinks(tmp_path, preview_env, name, exists):
    directory = tmp_path / "data"
    directory.mkdir()
    outside = tmp_path / "outside"
    if exists:
        outside.write_text("outside data")
    (directory / name).symlink_to(outside)
    with pytest.raises((preview.PreviewError, OSError)):
        preview.prepare(directory)
    assert outside.exists() is exists
    if exists:
        assert outside.read_text() == "outside data"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PRAXIMESH_MODE", "production"),
        ("DATABASE_URL", "postgresql://not-the-preview/database"),
        ("JUDGE_ENABLED", "1"),
        ("COACHING_ENABLED", "1"),
    ],
)
def test_preview_cannot_replace_production_or_judge_startup(
    tmp_path, preview_env, monkeypatch, key, value
):
    monkeypatch.setenv(key, value)
    with pytest.raises(preview.PreviewError):
        preview.prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_main_does_not_exec_web_after_failed_initialization(monkeypatch, capsys):
    def failed_prepare(directory):
        raise preview.PreviewError("initialization failed")

    monkeypatch.setattr(preview, "prepare", failed_prepare)
    monkeypatch.setattr(os, "execvpe", lambda *args: pytest.fail("Web must not start"))
    assert preview.main() == 1
    assert "initialization failed" in capsys.readouterr().err
