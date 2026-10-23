"""Coordinator control-flow tests only: no real database, proxy or judge evidence.

The real PostgreSQL/containers/WASI acceptance run is a separate required gate.
These fakes record ordering and durable intent across simulated process crashes.
"""

import copy
import json
import os
import stat
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import deploy

MANIFEST = {
    "images": {"web": "sha256:" + "a" * 64, "worker": "sha256:" + "b" * 64},
    "judge": {"bundle_sha256": "c" * 64, "policy_sha256": "d" * 64},
}


@pytest.fixture
def private_config(tmp_path):
    value = {
        "format": 1,
        "instance": "example",
        "database": {
            "container": "example-postgres",
            "owner_user": "deployment_owner",
            "owner_password": "test-owner-" + "a" * 32,
            "web_password": "test-web-" + "b" * 32,
            "worker_password": "test-worker-" + "c" * 32,
        },
        "networks": {"backend": "example-internal", "frontend": "example-ingress"},
        "application": {
            "secret_key": "test-web-secret-" + "d" * 50,
            "worker_secret": "test-worker-secret-" + "e" * 50,
            "registration_open": False,
        },
        "proxy": {"origin": "http://127.0.0.1:8080", "bind": "127.0.0.1"},
    }
    path = tmp_path / "deployment-config.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def test_private_configuration_accepts_explicit_distinct_credentials(private_config):
    path, value = private_config
    assert deploy.configuration(path) == value


@pytest.mark.parametrize(
    "field,value",
    [
        (("format",), True),
        (("instance",), "example/../../other"),
        (("database",), []),
        (("networks",), "frontend"),
        (("application",), None),
        (("database", "owner_password"), "too-short"),
        (("database", "web_password"), "REPLACE" + "x" * 30),
        (("database", "worker_password"), "test-web-" + "b" * 32),
        (("database", "owner_user"), "owner; DROP DATABASE example"),
        (("database", "container"), "--privileged"),
        (("networks", "frontend"), "network with spaces"),
        (("application", "registration_open"), "false"),
        (("application", "secret_key"), 123456),
        (("application", "secret_key"), "short"),
        (("application", "secret_key"), "REPLACE" + "x" * 60),
        (("application", "worker_secret"), "test-web-secret-" + "d" * 50),
    ],
)
def test_configuration_rejects_ambiguous_types_identifiers_and_credentials(
    private_config, field, value
):
    path, config = private_config
    parent = config
    for key in field[:-1]:
        parent = parent[key]
    parent[field[-1]] = value
    path.write_text(json.dumps(config))
    with pytest.raises(deploy.DeploymentError):
        deploy.configuration(path)


@pytest.mark.parametrize("invalid", [[], {"format": 1}, {"format": 1, "unknown": True}])
def test_configuration_requires_exact_top_level_schema(private_config, invalid):
    path, _ = private_config
    path.write_text(json.dumps(invalid))
    with pytest.raises(deploy.DeploymentError):
        deploy.configuration(path)


def test_configuration_rejects_group_readable_or_symlinked_secret_file(private_config, tmp_path):
    path, _ = private_config
    path.chmod(0o640)
    with pytest.raises(deploy.DeploymentError, match="private|0600"):
        deploy.configuration(path)
    path.chmod(0o600)
    link = tmp_path / "linked-config.json"
    link.symlink_to(path)
    with pytest.raises(deploy.DeploymentError, match="regular private"):
        deploy.configuration(link)


class Crash(BaseException):
    """Abrupt death bypasses normal exception recovery, as SIGKILL would."""


class FakeProxy:
    def __init__(self, host):
        self.host = host
        self.active = 21000

    def probe(self):
        self.host.event("proxy_probe")

    def maintenance(self):
        self.active = None
        self.host.event("maintenance")

    def activate(self, port):
        # Exposure can have occurred before the reload call returns an error.
        self.active = port
        target = self.host.state["current"]
        assert self.host.durable[-1]["operation"]["exposure_target"] == target["id"]
        self.host.event("activate", port)


class FakeDatabase:
    def __init__(self, host):
        self.host = host
        self.rows = {"old-db": ["existing-user-write"]}
        self.frozen = set()

    def name(self, generation):
        return "candidate-db-" + generation

    def ensure_roles(self):
        self.host.event("roles")

    def freeze(self, name):
        self.frozen.add(name)
        self.host.event("freeze", name)

    def thaw(self, name):
        self.frozen.discard(name)
        self.host.event("thaw", name)

    def backup_restore(self, source, target, path):
        assert source in self.frozen
        assert self.host.proxy.active is None
        self.rows[target] = copy.deepcopy(self.rows[source])
        self.host.event("backup_restore", source, target)
        return {"verified": True}

    def create(self, name):
        self.rows[name] = []
        self.host.event("create", name)

    def grant(self, name):
        self.host.event("grant", name)

    def invalidate_health(self, name):
        self.host.event("invalidate_health", name)


class Harness(deploy.Deployment):
    def __init__(self, directory, initial=False):
        self.directory = directory
        self.config = {"instance": "example"}
        self.events = []
        self.hook = lambda event: None
        self.durable = []
        self.proxy = FakeProxy(self)
        self._database = FakeDatabase(self)
        self.old = {
            "id": "old",
            "database": "old-db",
            "web": "old-web",
            "worker": "old-worker",
            "manifest": copy.deepcopy(MANIFEST),
        }
        self.state = {
            "site_id": "test-site",
            "current": None if initial else copy.deepcopy(self.old),
            "operation": None if initial else {"phase": "active"},
        }
        self.running = set() if initial else {("old", "web"), ("old", "worker")}
        if initial:
            self.proxy.active = None

    def event(self, *event):
        self.events.append(event)
        self.hook(event)

    def save(self, phase=None):
        if phase is not None:
            self.state["operation"]["phase"] = phase
        self.durable.append(copy.deepcopy(self.state))
        self.event("save", self.state["operation"]["phase"])

    def run(self, *arguments, **options):
        self.event("docker", *arguments)
        return b""

    def networks(self):
        self.event("networks")

    def cleanup_controls(self):
        self.event("cleanup_controls")

    def control(self, generation, *command, **options):
        self.event("control", generation["id"], *command)
        return b"{}"

    def stop(self, generation):
        if generation:
            self.running.discard((generation["id"], "web"))
            self.running.discard((generation["id"], "worker"))
            self.event("stop", generation["id"])

    def service(self, generation, role):
        self.running.add((generation["id"], role))
        self.event("service", generation["id"], role)

    def port(self, generation):
        return 21000 if generation["id"] == "old" else 21001

    def ready(self, generation):
        return (generation["id"], "worker") in self.running

    def wait_ready(self, generation):
        assert self.ready(generation)
        self.event("ready", generation["id"])

    def check(self, generation, token):
        assert self.proxy.active is None
        assert (generation["id"], "web") in self.running
        assert (generation["id"], "worker") in self.running
        self.event("check", generation["id"])

    def health(self, generation, *, external):
        self.event("health", generation["id"], external)

    def reboot(self):
        self.state = copy.deepcopy(self.durable[-1])
        self.hook = lambda event: None
        self.events.clear()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "verify", lambda *args: copy.deepcopy(MANIFEST))
    monkeypatch.setattr(deploy, "inspect_images", lambda *args: None)
    return Harness(tmp_path)


def index(events, prefix):
    return next(position for position, event in enumerate(events) if event[: len(prefix)] == prefix)


def fail_once(harness, predicate, error):
    def hook(event):
        if predicate(event):
            harness.hook = lambda event: None
            raise error

    harness.hook = hook


def test_upgrade_orders_drain_quiescence_clone_validation_and_durable_exposure(harness):
    harness.apply(harness.directory, "release-digest")
    candidate = harness.state["current"]
    assert candidate["id"] != "old"
    stages = [
        ("control", "old", "judge_admission", "drain"),
        ("maintenance",),
        ("stop", "old"),
        ("freeze", "old-db"),
        ("backup_restore", "old-db", candidate["database"]),
        ("control", candidate["id"], "migrate"),
        ("check", candidate["id"]),
        ("save", "exposing"),
        ("activate", 21001),
        ("save", "active"),
    ]
    positions = [index(harness.events, stage) for stage in stages]
    assert positions == sorted(positions)
    assert harness.database.rows[candidate["database"]] == ["existing-user-write"]
    assert "old-db" in harness.database.frozen
    assert harness.state["operation"]["exposure_target"] == candidate["id"]


def test_drain_failure_keeps_reading_and_services_until_explicit_successful_recovery(harness):
    fail_once(
        harness,
        lambda event: event[:4] == ("control", "old", "judge_admission", "drain"),
        deploy.DeploymentError("drain timed out"),
    )
    with pytest.raises(deploy.DeploymentError):
        harness.apply(harness.directory, "release-digest")
    assert harness.state["operation"]["phase"] == "drain_failed"
    assert harness.proxy.active == 21000
    assert harness.running == {("old", "web"), ("old", "worker")}
    assert not any(event[0] in {"maintenance", "stop", "freeze"} for event in harness.events)
    harness.events.clear()
    harness.recover()
    assert harness.state["operation"]["phase"] == "rolled_back"
    assert harness.state["current"]["id"] == "old"
    assert index(harness.events, ("control", "old", "judge_admission", "drain")) < index(
        harness.events, ("stop", "old")
    )
    assert not any(event[0] == "backup_restore" for event in harness.events)


def test_failed_candidate_validation_recovers_original_database_before_any_exposure(harness):
    fail_once(
        harness,
        lambda event: event[0] == "check" and event[1] != "old",
        deploy.DeploymentError("candidate failed"),
    )
    with pytest.raises(deploy.DeploymentError):
        harness.apply(harness.directory, "release-digest")
    assert harness.state["current"]["id"] == "old"
    assert harness.state["operation"]["phase"] == "rolled_back"
    assert harness.database.rows["old-db"] == ["existing-user-write"]
    assert [event for event in harness.events if event[0] == "activate"] == [("activate", 21000)]
    assert (harness.state["operation"]["candidate"]["id"], "worker") not in harness.running


def test_ambiguous_exposure_keeps_candidate_writes_and_never_restores_old_snapshot(harness):
    def after_exposure(event):
        if event == ("activate", 21001):
            target = harness.state["current"]
            harness.database.rows[target["database"]].append("new-user-write")
            harness.hook = lambda event: None
            raise deploy.DeploymentError("reload reply lost")

    harness.hook = after_exposure
    with pytest.raises(deploy.DeploymentError):
        harness.apply(harness.directory, "release-digest")
    target = copy.deepcopy(harness.state["current"])
    assert harness.state["operation"]["phase"] == "needs_recovery"
    assert harness.proxy.active is None
    harness.reboot()
    harness.recover()
    assert harness.state["current"]["id"] == target["id"]
    assert harness.database.rows[target["database"]] == ["existing-user-write", "new-user-write"]
    assert not any(event[0] == "backup_restore" for event in harness.events)
    assert ("thaw", "old-db") not in harness.events
    assert harness.state["operation"]["phase"] == "active"


@pytest.mark.parametrize(
    "phase", ["prepared", "draining", "quiescing", "copying", "migrating", "validating"]
)
def test_hard_crash_before_exposure_recovers_original_generation(harness, phase):
    fail_once(harness, lambda event: event == ("save", phase), Crash())
    with pytest.raises(Crash):
        harness.apply(harness.directory, "release-digest")
    assert harness.durable[-1]["operation"]["exposure_target"] is None
    harness.reboot()
    harness.recover()
    assert harness.state["current"]["id"] == "old"
    assert harness.state["operation"]["phase"] == "rolled_back"
    assert not any(event[0] == "backup_restore" for event in harness.events)
    assert ("activate", 21001) not in harness.events


def test_crash_after_exposure_intent_recovers_forward_even_before_proxy_reply(harness):
    fail_once(harness, lambda event: event == ("save", "exposing"), Crash())
    with pytest.raises(Crash):
        harness.apply(harness.directory, "release-digest")
    target = harness.durable[-1]["current"]
    assert target["id"] != "old"
    assert not any(event[0] == "activate" for event in harness.events)
    harness.reboot()
    harness.recover()
    assert harness.state["current"]["id"] == target["id"]
    assert ("activate", 21001) in harness.events
    assert ("thaw", "old-db") not in harness.events


def test_forward_recovery_drain_failure_stays_closed_and_preserves_current_database(harness):
    harness.apply(harness.directory, "release-digest")
    target = copy.deepcopy(harness.state["current"])
    harness.database.rows[target["database"]].append("new-user-write")
    harness.events.clear()
    fail_once(
        harness,
        lambda event: event[:4] == ("control", target["id"], "judge_admission", "drain"),
        deploy.DeploymentError("recovery drain timed out"),
    )
    with pytest.raises(deploy.DeploymentError, match="recovery drain timed out"):
        harness.recover()
    assert harness.proxy.active is None
    assert harness.state["current"]["id"] == target["id"]
    assert harness.state["operation"]["phase"] == "recovering_current"
    assert ("stop", target["id"]) not in harness.events
    assert ("thaw", "old-db") not in harness.events
    harness.reboot()
    harness.recover()
    assert harness.database.rows[target["database"]] == ["existing-user-write", "new-user-write"]
    assert harness.state["current"]["id"] == target["id"]


def test_crash_while_exposing_restored_previous_tracks_that_database_as_current(harness):
    def fail_candidate_then_crash_rollback(event):
        if event[0] == "check" and event[1] != "old":
            raise deploy.DeploymentError("candidate failed")
        if event == ("activate", 21000):
            harness.database.rows["old-db"].append("write-after-rollback-exposure")
            raise Crash()

    harness.hook = fail_candidate_then_crash_rollback
    with pytest.raises(Crash):
        harness.apply(harness.directory, "release-digest")
    assert harness.durable[-1]["operation"]["exposure_target"] == "old"
    harness.reboot()
    harness.recover()
    assert harness.state["current"]["id"] == "old"
    assert harness.database.rows["old-db"] == [
        "existing-user-write",
        "write-after-rollback-exposure",
    ]
    assert ("activate", 21001) not in harness.events
    assert not any(event[0] == "backup_restore" for event in harness.events)


def test_first_install_failure_remains_maintenance_and_retains_candidate_database(harness):
    harness.state["current"] = None
    harness.state["operation"] = None
    harness.proxy.active = None
    harness.running.clear()
    fail_once(
        harness, lambda event: event[0] == "check", deploy.DeploymentError("candidate failed")
    )
    with pytest.raises(deploy.DeploymentError):
        harness.apply(harness.directory, "release-digest", initial_content=True)
    candidate = harness.state["operation"]["candidate"]
    assert harness.state["operation"]["phase"] == "failed_initial"
    assert harness.state["current"] is None
    assert harness.proxy.active is None and not harness.running
    assert candidate["database"] in harness.database.rows
    assert not any(event[0] == "activate" for event in harness.events)


def test_incomplete_operation_cannot_be_overwritten_by_new_apply(harness):
    harness.state["operation"] = {"phase": "needs_recovery", "id": "keep-this-operation"}
    before = copy.deepcopy(harness.state)
    with pytest.raises(deploy.DeploymentError, match="requires recover"):
        harness.apply(harness.directory, "release-digest")
    assert harness.state == before and harness.events == []


def test_recovery_rejects_inconsistent_exposure_identity_before_any_route_change(harness):
    harness.state["operation"] = {"phase": "exposing", "exposure_target": "different"}
    with pytest.raises(deploy.DeploymentError, match="identity mismatch"):
        harness.recover()
    assert harness.proxy.active == 21000
    assert not any(event[0] in {"maintenance", "stop", "activate"} for event in harness.events)


def test_control_timeout_removes_only_its_owned_management_container(harness, monkeypatch):
    monkeypatch.setattr(harness, "environment", lambda *args: {})
    monkeypatch.setattr(harness, "runtime", lambda generation, role, name: ["--name", name])
    monkeypatch.setattr(harness, "inspect", lambda name: {"owned": True})
    calls = []

    def run(*arguments, **options):
        calls.append(arguments)
        if arguments[0] == "run":
            raise deploy.DeploymentError("CLI timeout")
        return b""

    monkeypatch.setattr(harness, "run", run)
    with pytest.raises(deploy.DeploymentError, match="CLI timeout"):
        deploy.Deployment.control(harness, harness.old, "judge_admission", "pause")
    assert len(calls) == 2
    assert calls[1] == ("rm", "--force", "--volumes", calls[0][2])


@pytest.fixture
def diagnostic_control(private_config, tmp_path, monkeypatch):
    _, config = private_config
    host = deploy.Deployment.__new__(deploy.Deployment)
    host.config = config
    host.directory = tmp_path / "private-state"
    host.directory.mkdir(mode=0o700)
    monkeypatch.setattr(host, "environment", lambda *args: {"PRIVATE_ENV": "env-only-marker"})
    monkeypatch.setattr(host, "runtime", lambda generation, role, name: ["--name", name])
    monkeypatch.setattr(host, "inspect", lambda name: {"owned": True})

    def fail(failure):
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            if arguments[1] == "run":
                if callable(failure):
                    return failure(arguments, options)
                raise failure
            return SimpleNamespace(stdout=b"")

        monkeypatch.setattr(deploy.subprocess, "run", run)
        with pytest.raises(deploy.DeploymentError) as caught:
            host.control(
                {"id": "test-generation", "manifest": MANIFEST}, "migrate", "argv-only-marker"
            )
        assert calls[-1] == ["docker", "rm", "--force", "--volumes", calls[0][3]]
        return caught.value

    return host, fail


def test_failed_control_captures_real_process_output_privately_and_redacts_secrets(
    diagnostic_control, monkeypatch, capsys
):
    host, fail = diagnostic_control
    # Credentials may contain each other and URL/JSON/Python escape characters.
    short = host.config["database"]["owner_password"]
    host.config["database"]["web_password"] = short + "sensitive-suffix"
    special = 'secret/ +"\\\n非ASCII' + "z" * 50
    host.config["application"]["secret_key"] = special
    credentials = [
        host.config["database"][key]
        for key in ("owner_password", "web_password", "worker_password")
    ] + [special, host.config["application"]["worker_secret"]]
    representations = [
        *credentials,
        deploy.urllib.parse.quote(special, safe=""),
        deploy.urllib.parse.quote_plus(special, safe=""),
        json.dumps(special, ensure_ascii=True)[1:-1],
        json.dumps(special, ensure_ascii=False)[1:-1],
        repr(special)[1:-1],
    ]
    stdout = "migration stdout marker\n" + "\n".join(representations)
    stderr = "Actual migration exception marker\n" + special
    real_run = deploy.subprocess.run

    def real_failure(arguments, options):
        program = (
            f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit(19)"
        )
        return real_run([sys.executable, "-c", program, "argv-only-marker"], **options)

    error = fail(real_failure)
    path = host.directory / error.diagnostic
    assert list(host.directory.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.getuid()
    data = json.loads(path.read_text())
    assert data["command"] == "migrate" and data["failure"] == "exit"
    assert data["exit_code"] == 19
    assert "migration stdout marker" in data["stdout"]
    assert "Actual migration exception marker" in data["stderr"]
    assert not data["stdout_truncated"] and not data["stderr_truncated"]
    captured = data["stdout"] + data["stderr"]
    assert "[REDACTED]" in captured
    assert all(secret not in captured for secret in representations)
    assert "sensitive-suffix" not in captured
    assert "argv-only-marker" not in path.read_text()
    assert "env-only-marker" not in path.read_text()
    assert "Actual migration exception marker" not in str(error)

    def raise_error():
        raise error

    controller = SimpleNamespace(
        locked=nullcontext,
        database=SimpleNamespace(operation_lock=nullcontext),
        maintain=raise_error,
    )
    monkeypatch.setattr(deploy, "Deployment", lambda *args: controller)
    monkeypatch.setattr(deploy, "configuration", lambda *args: {})
    monkeypatch.setattr(
        sys, "argv", ["deploy.py", "--config", "private", "--state", "private", "maintain"]
    )
    with pytest.raises(SystemExit) as exited:
        deploy.main()
    assert exited.value.code == 1
    output = capsys.readouterr().err
    assert error.diagnostic in output
    assert "Actual migration exception marker" not in output and special not in output


def test_control_timeout_diagnostic_redacts_before_truncating_and_bounds_json(diagnostic_control):
    host, fail = diagnostic_control
    secret = host.config["database"]["owner_password"]
    limit = deploy.CONTROL_DIAGNOSTIC_STREAM_BYTES
    stdout = "prefix:" * 100 + secret + "X" * (limit - len(secret) // 2)
    stderr = "\0" * (limit * 2) + "partial timeout output"
    error = fail(
        deploy.subprocess.TimeoutExpired(
            ["argv-only-marker"], 1, output=stdout.encode(), stderr=stderr.encode()
        )
    )
    path = host.directory / error.diagnostic
    data = json.loads(path.read_text())
    assert data["failure"] == "timeout" and data["exit_code"] is None
    assert data["stdout_truncated"] and data["stderr_truncated"]
    assert secret[-len(secret) // 2 :] not in data["stdout"]
    assert data["stderr"].endswith("partial timeout output")
    assert path.stat().st_size <= deploy.CONTROL_DIAGNOSTIC_MAX_BYTES


def test_control_diagnostic_redacts_optional_provider_token(diagnostic_control):
    host, fail = diagnostic_control
    token = "synthetic-model-token/with+reserved=characters"
    host.config["application"]["coaching"] = {"token": token}
    encoded = deploy.urllib.parse.quote(token, safe="")
    error = fail(
        deploy.subprocess.CalledProcessError(
            1, ["argv-only-marker"], stderr=f"synthetic failure: {token} {encoded}".encode()
        )
    )
    text = (host.directory / error.diagnostic).read_text()
    assert token not in text and encoded not in text
    assert text.count("[REDACTED]") == 2


def test_control_diagnostic_write_failure_still_cleans_container(diagnostic_control, monkeypatch):
    host, fail = diagnostic_control

    def broken_write(path, value):
        path.write_text("partial diagnostic")
        raise OSError("private storage detail must not escape")

    monkeypatch.setattr(deploy, "atomic_json", broken_write)
    error = fail(
        deploy.subprocess.CalledProcessError(7, ["argv-only-marker"], output="failure", stderr=None)
    )
    assert error.diagnostic is None
    assert "private diagnostic could not be written" in str(error)
    assert "private storage detail" not in str(error)
    assert list(host.directory.iterdir()) == []


@pytest.mark.parametrize("invalid", ["permissions", "missing", "symlink"])
def test_control_diagnostic_requires_existing_private_directory(
    diagnostic_control, tmp_path, invalid
):
    host, fail = diagnostic_control
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if invalid == "permissions":
        host.directory.chmod(0o750)
    else:
        host.directory.rmdir()
        if invalid == "symlink":
            host.directory.symlink_to(outside, target_is_directory=True)
    error = fail(deploy.subprocess.CalledProcessError(7, ["argv-only-marker"], stderr=b"failure"))
    assert error.diagnostic is None
    assert not list(outside.iterdir())
    if host.directory.exists():
        assert not list(host.directory.iterdir())


def test_other_docker_failures_do_not_capture_output(diagnostic_control, monkeypatch):
    host, _ = diagnostic_control

    def fail(*args, **kwargs):
        raise deploy.subprocess.CalledProcessError(
            1, ["argv-only-marker"], output=b"private output"
        )

    monkeypatch.setattr(deploy.subprocess, "run", fail)
    with pytest.raises(deploy.DeploymentError, match="container operation failed") as caught:
        host.run("inspect", "owned")
    assert caught.value.diagnostic is None
    assert list(host.directory.iterdir()) == []


def test_inspection_refuses_foreign_container_before_control(harness, monkeypatch):
    response = SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Config": {"Labels": {"org.praximesh.site": "another-site"}}}]),
    )
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: response)
    with pytest.raises(deploy.DeploymentError, match="another deployment"):
        deploy.Deployment.inspect(harness, "foreign-container")
    assert harness.events == []


def test_readiness_allows_stable_historical_restart_count(harness, monkeypatch):
    readiness = iter([False, True])
    checked = []

    def ready(generation):
        checked.append(generation["id"])
        return next(readiness)

    monkeypatch.setattr(harness, "ready", ready)
    monkeypatch.setattr(
        harness,
        "inspect",
        lambda name: {"RestartCount": 3, "State": {"Running": True, "Status": "running"}},
    )
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    deploy.Deployment.wait_ready(harness, harness.old)
    assert checked == ["old", "old"]


def test_readiness_rejects_a_new_restart_during_current_wait(harness, monkeypatch):
    inspections = []

    def inspect(name):
        inspections.append(name)
        assert len(inspections) <= 3, "A newly restarting candidate must fail promptly"
        return {
            "RestartCount": 3 if len(inspections) == 1 else 4,
            "State": {"Running": True, "Status": "running"},
        }

    monkeypatch.setattr(harness, "ready", lambda generation: False)
    monkeypatch.setattr(harness, "inspect", inspect)
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    with pytest.raises(deploy.DeploymentError, match="fresh startup checks"):
        deploy.Deployment.wait_ready(harness, harness.old)
    assert len(inspections) >= 2
