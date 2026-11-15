"""Real private files, original locks, immutable sources and role separation."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.compose_bootstrap import configuration_digest
from core.compose_bootstrap_state import InstallationState, initialize
from scripts.deploy_database import DeploymentDatabaseError
from scripts.prepare_compose import (
    application_environment,
    dotenv,
    make_installation,
    publish,
    sources,
)


def test_original_host_binding_survives_reopen_and_excludes_another_holder(tmp_path):
    directory = tmp_path / "bootstrap"
    initialize(directory, "a" * 64)
    with InstallationState(directory, "a" * 64).locked() as first:
        assert first.check() is False
        with pytest.raises(BlockingIOError), InstallationState(directory, "a" * 64).locked():
            pytest.fail("A second holder entered the original lock")
        first.bind()
    with InstallationState(directory, "a" * 64).locked() as reopened:
        assert reopened.check() is True


@pytest.mark.parametrize(
    "defect", ["identity", "state", "lock", "configuration", "moved-directory"]
)
def test_missing_or_changed_host_identity_cannot_reinitialize_it(tmp_path, defect):
    directory = tmp_path / "bootstrap"
    initialize(directory, "a" * 64)
    with InstallationState(directory, "a" * 64).locked() as state:
        state.bind()
    digest = "a" * 64
    if defect == "configuration":
        digest = "b" * 64
    elif defect == "moved-directory":
        # Copying identity bytes to a new directory cannot claim the old inode.
        import shutil

        target = tmp_path / "replacement"
        shutil.copytree(directory, target)
        directory = target
    else:
        name = {"identity": "identity.json", "state": "state.json", "lock": "bootstrap.lock"}[
            defect
        ]
        (directory / name).unlink()
    with (
        pytest.raises((DeploymentDatabaseError, FileNotFoundError)),
        InstallationState(directory, digest).locked(),
    ):
        pytest.fail("Changed installation identity was accepted")


def test_replacing_original_lock_during_publication_is_detected(tmp_path):
    directory = tmp_path / "bootstrap"
    initialize(directory, "a" * 64)
    with pytest.raises(DeploymentDatabaseError, match="lock changed"):
        with InstallationState(directory, "a" * 64).locked() as state:
            (directory / "bootstrap.lock").rename(directory / "original.lock")
            fd = os.open(directory / "bootstrap.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            state.bind()
    assert json.loads((directory / "state.json").read_text())["bound"] is False


def test_completed_environment_is_never_replaced_or_recreated(tmp_path):
    path = tmp_path / "web.env"
    original = b"PRIVATE_KEY='original-secret'\n"
    publish(path, original)
    publish(path, original, existing=True)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="changed"):
        publish(path, b"PRIVATE_KEY='replacement'\n", existing=True)
    assert path.read_bytes() == original
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        publish(path, original, existing=True)
    assert not path.exists()


def test_frozen_source_modes_ignore_umask_without_widening_private_installation(tmp_path):
    directory = tmp_path / "source"
    original = os.umask(0o077)
    try:
        contents = {"core/management/commands/example.py": b"# Frozen reviewed source\n"}
        sources(directory, contents, existing=False)
        sources(directory, contents, existing=True)
    finally:
        os.umask(original)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "core/management/commands").stat().st_mode & 0o777 == 0o755
    assert (directory / "core/management/commands/example.py").stat().st_mode & 0o777 == 0o644
    (directory / "unknown.txt").write_text("Unrelated file")
    with pytest.raises(ValueError, match="Unexpected files"):
        sources(directory, contents, existing=True)
    assert (directory / "unknown.txt").read_text() == "Unrelated file"


def test_prepared_role_files_do_not_disclose_database_owner_or_other_role_secrets(tmp_path):
    options = SimpleNamespace(
        state=tmp_path,
        release=Path("/frozen/release"),
        sha256="a" * 64,
        bind="0.0.0.0",
        port=8000,
        ai=True,
        gpu=None,
        empty=False,
    )
    release = {"source_id": "b" * 64, "judge": {"bundle_sha256": "c" * 64}}
    engine = SimpleNamespace(path=Path("/private/docker.sock"), identity=(12, 34))
    saved = make_installation(options, release, engine, {"ID": "test-private-daemon"})
    assert len(configuration_digest(saved["bootstrap"])) == 64
    web = dotenv(application_environment(saved, "web"))
    worker = dotenv(application_environment(saved, "worker"))
    database = saved["bootstrap"]["database"]
    for value in (web, worker):
        assert database["owner_password"].encode() not in value
        assert saved["bootstrap"]["application_secret"].encode() not in value
    assert database["worker_password"].encode() not in web
    assert database["web_password"].encode() not in worker
    assert saved["web_secret"].encode() not in worker
    assert b"COACHING_TOKEN" not in worker


@pytest.mark.parametrize(
    "name,value", [("INVALID-NAME", "x"), ("TOKEN", "x\ny"), ("TOKEN", "x\0y")]
)
def test_environment_cannot_inject_additional_fields(name, value):
    with pytest.raises(ValueError):
        dotenv({name: value})
