"""Harness control/fixture tests only: no browser, model or learner code executes."""

import builtins
import copy
import hashlib
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from scripts import advanced_browsers
from scripts.browser_acceptance import bootstrap_moderator, cleanup_moderator
from scripts.release_acceptance import browser_environment


def test_release_browser_environment_disables_inherited_model_and_tls_fixture():
    original = {
        "BROWSER_COACHING": "1",
        "BROWSER_COACHING_REAL": "1",
        "BROWSER_COACHING_OTHER": "unexpected",
        "BROWSER_TEST_SPKI": "old",
        "BROWSER_CA_FILE": "/old/ca.pem",
        "PLAYWRIGHT_BROWSERS_PATH": "/private/cache",
    }
    before = copy.deepcopy(original)
    result = browser_environment(original, "http://127.0.0.1:1234")
    assert original == before
    assert {key: value for key, value in result.items() if key.startswith("BROWSER_COACHING")} == {
        "BROWSER_COACHING": "0"
    }
    assert "BROWSER_TEST_SPKI" not in result and "BROWSER_CA_FILE" not in result
    assert result["BROWSER_JUDGE"] == "1" and result["BROWSER_URL"] == "http://127.0.0.1:1234"
    assert result["PLAYWRIGHT_BROWSERS_PATH"] == "/private/cache"


@pytest.fixture
def trusted_owner(monkeypatch):
    """Execute only the harness's fixed Django fixture commands in the test DB."""
    calls = []

    def owner(*arguments, env):
        assert arguments[:3] == ("manage.py", "shell", "-c")
        calls.append((arguments, env.copy()))
        with monkeypatch.context() as scoped:
            for key, value in env.items():
                assert key.startswith("BROWSER_")
                scoped.setenv(key, value)
            call_command("shell", "-c", arguments[3], stdout=io.StringIO())
        return b""

    return owner, calls


@pytest.mark.django_db
def test_reviewer_bootstrap_atomic_and_cleanup_idempotent(trusted_owner):
    owner, calls = trusted_owner
    credentials = bootstrap_moderator(owner, {})
    assert len(calls) == 1
    user = get_user_model().objects.get(username=credentials["BROWSER_MODERATOR_USERNAME"])
    assert user.check_password(credentials["BROWSER_MODERATOR_PASSWORD"])
    assert set(user.user_permissions.values_list("content_type__app_label", "codename")) == {
        ("community", "moderate_content")
    }
    assert credentials["BROWSER_MODERATOR_PASSWORD"] not in repr(calls[0][0])
    cleanup_moderator(owner, {}, credentials)
    assert not get_user_model().objects.filter(pk=user.pk).exists()
    cleanup_moderator(owner, {}, credentials)


@pytest.mark.django_db
def test_failed_reviewer_grant_rolls_back_account(trusted_owner, monkeypatch):
    owner, _ = trusted_owner
    count = get_user_model().objects.count()

    def fail(*args, **kwargs):
        assert args[0] == "community_moderator"
        raise CommandError("Synthetic grant failure")

    monkeypatch.setattr("django.core.management.call_command", fail)
    with pytest.raises(CommandError, match="Synthetic grant failure"):
        bootstrap_moderator(owner, {})
    assert get_user_model().objects.count() == count


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["password", "privilege", "replacement"])
def test_reviewer_cleanup_preserves_account_without_exact_ownership(trusted_owner, change):
    owner, _ = trusted_owner
    credentials = bootstrap_moderator(owner, {})
    user = get_user_model().objects.get(username=credentials["BROWSER_MODERATOR_USERNAME"])
    if change == "replacement":
        user.delete()
        user = get_user_model().objects.create_user(
            username=credentials["BROWSER_MODERATOR_USERNAME"], password="another-owner-password"
        )
    elif change == "password":
        user.set_password("another-owner-password")
        user.save()
    else:
        user.is_staff = True
        user.save()
    with pytest.raises(CommandError, match="Synthetic reviewer ownership check failed"):
        cleanup_moderator(owner, {}, credentials)
    assert get_user_model().objects.filter(pk=user.pk).exists()


def test_reviewer_cleanup_refuses_nonfixture_before_owner_access():
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not access owner process")

    with pytest.raises(ValueError):
        cleanup_moderator(
            forbidden,
            {},
            {
                "BROWSER_MODERATOR_USERNAME": "real-moderator",
                "BROWSER_MODERATOR_PASSWORD": "a" * 48,
            },
        )


@pytest.mark.parametrize("mode", ["429_then_finished", "never_finished", "revoked", "late_result"])
def test_specialist_poll_interval_retry_scope_and_deadline(monkeypatch, mode):
    original_import = builtins.__import__

    def without_browser_dependencies(name, *args, **kwargs):
        if name.split(".")[0] in {"playwright", "browser_community"}:
            raise ModuleNotFoundError("Optional browser dependencies are unavailable", name=name)
        return original_import(name, *args, **kwargs)

    # A standard import guard also rejects already cached optional modules, so
    # this remains a default-install regression even in a browser-enabled venv.
    monkeypatch.setattr(builtins, "__import__", without_browser_dependencies)
    with pytest.raises(ModuleNotFoundError, match="Optional browser dependencies"):
        __import__("playwright.sync_api")
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        module = runpy.run_path(str(Path(__file__).with_name("browser_specialists.py")))
    finally:
        sys.path[:] = original_path
    clock, calls, sleeps = [0.0], [], []

    def request(path, *, token):
        assert path == "/api/v1/submissions/fixture/" and token == "private-test-token"
        calls.append(clock[0])
        if mode == "revoked":
            raise module["PracticeError"]("Token is invalid, expired or revoked.")
        if mode == "late_result":
            clock[0] = 241
            return {"status": "finished"}
        if mode == "429_then_finished":
            if len(calls) == 1:
                raise module["PracticeError"](
                    "Rate or pending-submission limit reached. Retry later."
                )
            if len(calls) == 3:
                return {"status": "finished"}
        return {"status": "running"}

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", sleep)
    wait = module["wait_finished"]
    api = SimpleNamespace(request=request)
    if mode == "429_then_finished":
        assert wait(api, "fixture", "private-test-token") == {"status": "finished"}
        assert calls == [0, 2, 4] and sleeps == [2, 2]
    elif mode == "revoked":
        with pytest.raises(module["PracticeError"], match="revoked"):
            wait(api, "fixture", "private-test-token")
        assert calls == [0] and sleeps == []
    else:
        with pytest.raises(AssertionError, match="did not finish"):
            wait(api, "fixture", "private-test-token")
        if mode == "never_finished":
            assert clock[0] == 240 and calls == list(range(0, 240, 2))
        else:
            assert calls == [0] and sleeps == []


@pytest.mark.parametrize("failure", [None, "browser_blogs", "browser_specialists", "organization"])
@pytest.mark.parametrize("include_api", [False, True])
def test_advanced_artifacts_failure_cleanup_and_import_path(
    monkeypatch, tmp_path, failure, include_api
):
    artifacts = tmp_path / "cohort"
    artifacts.mkdir()
    receipts = (
        "specialist-browser.json",
        "blogs-browser.json",
        "organizations-browser.json",
        "coaching-browser.json",
        "object-api-browser.json",
    )
    for name in receipts:
        (artifacts / name).write_text('{"passed":true}')
    original_path = sys.path[:]
    commands, cleaned = [], []
    credentials = {
        "BROWSER_MODERATOR_USERNAME": "community-reviewer-" + "a" * 12,
        "BROWSER_MODERATOR_PASSWORD": "b" * 48,
    }

    def owner(*args, **kwargs):
        assert not any((artifacts / name).exists() for name in receipts)
        commands.append("owner")

    def child(arguments, **kwargs):
        script = Path(arguments[1]).stem
        assert kwargs["env"]["BROWSER_ARTIFACTS"] == str(artifacts.resolve())
        commands.append(script)
        if script == failure:
            raise subprocess.TimeoutExpired(arguments, 600)

    def organizations(*args):
        assert args[2] == artifacts
        sys.path.insert(0, "synthetic nested import path")
        if failure == "organization":
            raise RuntimeError("Synthetic organization failure")

    monkeypatch.setattr(advanced_browsers.subprocess, "run", child)
    monkeypatch.setattr(advanced_browsers, "bootstrap_moderator", lambda *args: credentials)
    monkeypatch.setattr(
        advanced_browsers, "cleanup_moderator", lambda *args: cleaned.append(args[2])
    )
    monkeypatch.setattr(
        advanced_browsers.runpy, "run_path", lambda *args: {"run_journey": organizations}
    )
    if failure:
        with pytest.raises((subprocess.TimeoutExpired, RuntimeError)):
            advanced_browsers.run_advanced_browsers(
                {"BROWSER_JUDGE": "1"}, owner, artifacts, include_object_api=include_api
            )
    else:
        result = advanced_browsers.run_advanced_browsers(
            {"BROWSER_JUDGE": "1"}, owner, artifacts, include_object_api=include_api
        )
        assert result["journeys"] == [
            "browser_specialists",
            "browser_blogs",
            "browser_organizations",
            "browser_coaching",
        ] + (["browser_object_api"] if include_api else [])
    assert cleaned == ([] if failure == "browser_specialists" else [credentials])
    assert sys.path == original_path


@pytest.mark.parametrize("ready", [False, True])
def test_model_lifetime_includes_startup_and_does_not_add_termination_grace(
    monkeypatch, tmp_path, ready
):
    # Fake only process/network/clock interfaces: this is no model-runtime evidence.
    artifact = tmp_path / "synthetic-artifact"
    artifact.write_bytes(b"not executable or a model")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    state = tmp_path / "state"
    cpu = sorted(os.sched_getaffinity(0))[:2]
    assert len(cpu) == 2
    clock = [0.0]
    signals, waits, timeouts = [], [], []
    child = SimpleNamespace(pid=987654, alive=True)
    child.poll = lambda: None if child.alive else -signal.SIGKILL

    def wait(timeout):
        waits.append(timeout)
        if child.alive:
            raise subprocess.TimeoutExpired("synthetic model process", timeout)
        return -signal.SIGKILL

    child.wait = wait

    def killpg(pid, sig):
        assert pid == child.pid
        signals.append((clock[0], sig))
        if sig == signal.SIGKILL:
            child.alive = False

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return ("127.0.0.1", 12345)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_url(url, timeout):
        assert url == "http://127.0.0.1:12345/health"
        timeouts.append(timeout)
        assert 0 < timeout <= min(1, 30 - clock[0])
        if ready:
            return Response()
        clock[0] += timeout
        raise urllib.error.URLError("synthetic startup pending")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_coaching_server.py",
            "--binary",
            str(artifact),
            "--binary-sha256",
            digest,
            "--model",
            str(artifact),
            "--model-sha256",
            digest,
            "--state",
            str(state),
            "--cpu",
            str(cpu[0]),
            "--cpu",
            str(cpu[1]),
            "--lifetime",
            "30",
        ],
    )
    monkeypatch.setattr("signal.signal", lambda *args: None)
    monkeypatch.setattr("socket.socket", Socket)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("os.killpg", killpg)
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: SimpleNamespace(open=open_url))
    script = Path(__file__).parents[1] / "scripts/local_coaching_server.py"
    if ready:
        runpy.run_path(str(script), run_name="__main__")
    else:
        with pytest.raises(RuntimeError, match="startup deadline"):
            runpy.run_path(str(script), run_name="__main__")
    assert clock[0] == 30
    assert signals == [(30, signal.SIGTERM), (30, signal.SIGKILL)]
    assert waits == [0, 3]
    assert not (state / "runtime-connection.json").exists()
    assert not list(state.glob("px-ai-private-*"))
    cleanup = json.loads((state / "runtime-cleanup.json").read_text())
    assert cleanup["process_terminal"] and cleanup["private_connection_removed"]
