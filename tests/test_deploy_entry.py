"""Public command dispatch and pre-database boundaries; services are explicit spies."""

import copy
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import deploy

GPU_HISTORY = (
    "gpu_configuration",
    "gpu_runtime",
    "gpu_legacy_oom",
    "gpu_startup_aborts",
    "gpu_recoveries",
    "gpu_candidate_checks",
    "gpu_preparations",
    "gpu_exposures",
    "gpu_retirements",
    "gpu_releases",
    "gpu_workers",
    "gpu_checks",
)


@pytest.fixture
def entry(tmp_path, monkeypatch):
    class Entry(deploy.Deployment):
        def __init__(self):
            self.config = {"instance": "example"}
            self.state = {"instance": "example", "current": None, "operation": None}
            self.events = []
            self.held = False
            self.fail_coordinated = False

        @contextmanager
        def locked(self, **kwargs):
            assert not self.held
            self.held = True
            self.events.append("lock")
            try:
                yield
            finally:
                self.held = False

        def require_locked(self):
            assert self.held

        @property
        def database(self):
            self.events.append("database")
            return SimpleNamespace(operation_lock=self.database_lock)

        @contextmanager
        def database_lock(self):
            assert self.held
            self.events.append("database-lock")
            yield

        def apply(self, release, digest, initial_content=False):
            self.events.append(("cpu-apply", release, digest, initial_content))

        def apply_coordinated(self, release, digest, *, initial_content=False):
            self.events.append(("gpu-apply", release, digest, initial_content))

        def recover(self):
            self.events.append("cpu-recover")

        def recover_coordinated(self):
            self.events.append("gpu-recover")
            if self.fail_coordinated:
                raise deploy.DeploymentError("Original GPU recovery record is invalid")

    value = Entry()
    value.release = tmp_path / "release"
    value.manifest = {"format": 1}
    value.verify_error = False

    def verify(path, digest):
        assert value.held and path == value.release and digest == "a" * 64
        assert "database" not in value.events
        value.events.append("verify")
        if value.verify_error:
            raise ValueError("Frozen archive mismatch")
        return copy.deepcopy(value.manifest)

    monkeypatch.setattr(deploy, "verify", verify)
    monkeypatch.setattr(deploy, "configuration", lambda path: value.config)
    monkeypatch.setattr(deploy, "Deployment", lambda *args: value)

    def invoke(action="apply", *, initial_content=False):
        arguments = ["deploy.py", "--config", "private", "--state", str(tmp_path), action]
        if action == "apply":
            arguments += ["--release", str(value.release), "--sha256", "a" * 64]
            if initial_content:
                arguments.append("--initial-content")
        monkeypatch.setattr(sys, "argv", arguments)
        deploy.main()

    value.invoke = invoke
    return value


def attach(entry):
    entry.config["gpu"] = {"model_only": True}
    entry.state["gpu_attachment"] = {"phase": "attached"}


@pytest.mark.parametrize("bound", [False, True])
def test_cpu_command_without_gpu_history_keeps_original_path(entry, bound):
    if bound:
        attach(entry)
    entry.invoke()
    assert entry.events == [
        "lock",
        "verify",
        "database",
        "database-lock",
        ("cpu-apply", entry.release, "a" * 64, False),
    ]


@pytest.mark.parametrize("initial_content", [False, True])
def test_four_image_command_selects_complete_lifecycle(entry, initial_content):
    attach(entry)
    entry.manifest = {"format": 2, "gpu": {"model_only": True}}
    entry.invoke(initial_content=initial_content)
    assert entry.events == [
        "lock",
        "verify",
        "database",
        "database-lock",
        ("gpu-apply", entry.release, "a" * 64, initial_content),
    ]


@pytest.mark.parametrize("history", GPU_HISTORY)
@pytest.mark.parametrize("action", ["apply", "recover"])
def test_cpu_target_or_recovery_cannot_bypass_any_gpu_history(entry, history, action):
    attach(entry)
    entry.state[history] = {"original-record": True}
    entry.invoke(action)
    assert entry.events[-1] == (
        ("gpu-apply", entry.release, "a" * 64, False) if action == "apply" else "gpu-recover"
    )
    assert "cpu-recover" not in entry.events


@pytest.mark.parametrize("location", ["current", "previous", "candidate"])
def test_generation_manifest_requires_original_gpu_lifecycle(entry, location):
    attach(entry)
    generation = {"id": "original", "manifest": {"gpu": {"model_only": True}}}
    if location == "current":
        entry.state[location] = generation
    else:
        entry.state["operation"] = {"phase": "recovering", location: generation}
    entry.invoke("recover")
    assert entry.events[-1] == "gpu-recover"


@pytest.mark.parametrize("missing", ["configuration", "attachment", "both", "pending"])
def test_gpu_selection_without_completed_original_binding_never_accesses_database(entry, missing):
    attach(entry)
    entry.manifest = {"format": 2, "gpu": {"model_only": True}}
    if missing in {"configuration", "both"}:
        del entry.config["gpu"]
    if missing in {"attachment", "both"}:
        del entry.state["gpu_attachment"]
    if missing == "pending":
        entry.state["gpu_attachment"]["phase"] = "intended"
    with pytest.raises(SystemExit) as stopped:
        entry.invoke()
    assert stopped.value.code == 1
    assert "database" not in entry.events


def test_archive_failure_precedes_database_or_deployment(entry):
    entry.verify_error = True
    with pytest.raises(SystemExit) as stopped:
        entry.invoke()
    assert stopped.value.code == 1 and entry.events == ["lock", "verify"]


def test_invalid_gpu_recovery_never_falls_back_to_cpu(entry):
    attach(entry)
    entry.state["gpu_releases"] = {"unknown-operation": {}}
    entry.fail_coordinated = True
    with pytest.raises(SystemExit) as stopped:
        entry.invoke("recover")
    assert stopped.value.code == 1 and entry.events[-1] == "gpu-recover"
    assert "cpu-recover" not in entry.events


def test_cpu_recovery_and_status_preserve_existing_boundaries(entry, capsys):
    entry.invoke("recover")
    assert entry.events[-1] == "cpu-recover"
    entry.events.clear()
    capsys.readouterr()
    entry.invoke("status")
    assert entry.events == ["lock"]
    assert json.loads(capsys.readouterr().out)["instance"] == "example"
