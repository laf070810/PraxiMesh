"""Local compiler filesystem/CLI boundaries; no Docker, GPU or learner execution."""

import copy
import json
import os
import shutil
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest

from judge.container_compilation import CompilationCancelled
from judge.gpu_device_lock import DeviceLockError
from judge.sandbox import SandboxUnavailable
from scripts import practice, practice_cuda
from scripts.practice_cuda import CompilerWorkspace, LocalCompilerError
from tests.test_container_journal import Engine


@pytest.fixture
def engine():
    value = Engine()
    value.check_socket = lambda: None
    return value


@pytest.fixture
def exercise(tmp_path):
    problem = json.loads(Path("content/candidates/cuda.json").read_text())["problems"][0]
    package = {
        "format": 1,
        "version_id": 17,
        "source_digest": "a" * 64,
        "formal_result": False,
        "problem": {
            key: value
            for key, value in problem.items()
            if key not in {"reference", "tests", "attribution", "license"}
        },
        "public_tests": [
            {key: value for key, value in case.items() if key != "public"}
            for case in problem["tests"]
            if case["public"]
        ],
    }
    practice.validate_package(package)
    directory = tmp_path / "exercise"
    directory.mkdir(mode=0o700)
    (directory / "exercise.json").write_text(
        json.dumps({"server": "https://example.test", "package": package})
    )
    (directory / "solution.cu").write_text(problem["reference"])
    return directory, package


def command(tmp_path, directory):
    return [
        "compile",
        str(directory),
        "--state",
        str(tmp_path / "state"),
        "--docker-socket",
        "/unused/socket",
        "--compiler-image",
        "sha256:" + "a" * 64,
        "--architecture",
        "8.6",
    ]


def test_state_is_private_persistent_and_exclusive(tmp_path, engine):
    state = tmp_path / "state"
    with CompilerWorkspace(state, engine, create=True) as first:
        binding = copy.deepcopy(first.binding)
        assert first.record() is None
        with pytest.raises(DeviceLockError):
            with CompilerWorkspace(state, engine, create=True):
                pytest.fail("Concurrent ownership was allowed")
    with CompilerWorkspace(state, engine, create=False) as reopened:
        assert reopened.binding == binding and reopened.record() is None
    for path in (state, state / "locks", state / "journal"):
        assert path.stat().st_mode & 0o777 == 0o700
    assert (state / "compiler.json").stat().st_mode & 0o777 == 0o600
    assert not any(call[0] == "container" for call in engine.calls)


@pytest.mark.parametrize("create", [False, True])
def test_existing_unmarked_directory_is_never_adopted(tmp_path, engine, create):
    state = tmp_path / "existing"
    state.mkdir(mode=0o700)
    (state / "private-work").write_text("preserve")
    with pytest.raises(OSError):
        with CompilerWorkspace(state, engine, create=create):
            pytest.fail("Unmarked state was adopted")
    assert sorted(p.name for p in state.iterdir()) == ["private-work"]


@pytest.mark.parametrize("kind", ["copy", "symlink", "journal", "locks", "installation"])
def test_state_identity_replacement_refuses_without_adopting_it(tmp_path, engine, kind):
    state = tmp_path / "state"
    with CompilerWorkspace(state, engine, create=True):
        pass
    original = (state / "compiler.json").read_bytes()
    target = state
    if kind == "copy":
        target = tmp_path / "copy"
        shutil.copytree(state, target)
    elif kind == "symlink":
        target = tmp_path / "alias"
        target.symlink_to(state, target_is_directory=True)
    elif kind in {"journal", "locks"}:
        old = state / (kind + "-original")
        (state / kind).rename(old)
        shutil.copytree(old, state / kind)
    else:
        (state / "journal/installation.json").unlink()
    with pytest.raises((LocalCompilerError, SandboxUnavailable, OSError)):
        with CompilerWorkspace(target, engine, create=True):
            pytest.fail("Replacement state was adopted")
    assert (state / "compiler.json").read_bytes() == original
    if kind == "installation":
        assert not (state / "journal/installation.json").exists()
    assert not any(call[0] == "container" for call in engine.calls)


@pytest.mark.parametrize("kind", ["empty", "array", "boolean-version", "hardlink", "fifo"])
def test_untrusted_marker_fails_closed(tmp_path, engine, kind):
    state = tmp_path / "state"
    with CompilerWorkspace(state, engine, create=True):
        pass
    marker = state / "compiler.json"
    data = json.loads(marker.read_bytes())
    if kind == "hardlink":
        os.link(marker, state / "shared")
    elif kind == "fifo":
        marker.unlink()
        os.mkfifo(marker, 0o600)
    else:
        data["version"] = True
        marker.write_bytes(
            b"" if kind == "empty" else b"[]" if kind == "array" else json.dumps(data).encode()
        )
    with pytest.raises((LocalCompilerError, SandboxUnavailable, OSError)):
        with CompilerWorkspace(state, engine, create=True):
            pytest.fail("Invalid marker was adopted")
    assert not any(call[0] == "container" for call in engine.calls)


def test_change_is_detected_while_lock_is_held(tmp_path, engine):
    state = tmp_path / "state"
    with CompilerWorkspace(state, engine, create=True) as opened:
        (state / "compiler.json").write_bytes(b"{}")
        with pytest.raises(LocalCompilerError):
            opened.check()


def test_sigterm_is_deferred_to_safe_callback_and_handler_restored():
    original = signal.getsignal(signal.SIGTERM)
    with practice_cuda.deferred_signals() as check:
        os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(CompilationCancelled):
            check()
    assert signal.getsignal(signal.SIGTERM) == original


@pytest.mark.parametrize(
    "status,exit_code", [("compiled", 0), ("syntax_error", 1), ("platform_error", 2)]
)
def test_cli_distinguishes_compilation_from_examples_and_formal_results(
    tmp_path, exercise, monkeypatch, capsys, status, exit_code
):
    directory, package = exercise
    compiler = Mock(
        return_value={
            "status": status,
            "formal_result": False,
            "executed": False,
            "ready": False,
        }
    )
    monkeypatch.setattr(practice_cuda, "compile_source", compiler)
    assert practice.main(command(tmp_path, directory)) == exit_code
    result = json.loads(capsys.readouterr().out)
    assert result["version_id"] == 17 and result["formal_result"] is False
    assert result["executed"] is False and "verdict" not in result
    assert compiler.call_args.args == (
        (directory / "solution.cu").read_text(),
        package["problem"]["operation"],
        [8, 6],
    )


@pytest.mark.parametrize("field", ["docker_socket", "compiler_image", "device", "command"])
def test_package_cannot_choose_infrastructure(tmp_path, exercise, monkeypatch, capsys, field):
    directory, package = exercise
    package["problem"][field] = "untrusted"
    (directory / "exercise.json").write_text(
        json.dumps({"server": "https://example.test", "package": package})
    )
    engine = Mock(side_effect=AssertionError("Must reject before contacting Docker"))
    monkeypatch.setattr(practice_cuda, "LocalEngine", engine)
    assert practice.main(command(tmp_path, directory)) == 2
    engine.assert_not_called()
    assert not (tmp_path / "state").exists()
    assert "untrusted" not in capsys.readouterr().err


def test_cpu_package_never_enters_compiler(tmp_path, monkeypatch, capsys):
    engine = Mock(side_effect=AssertionError("Must reject CPU input before Docker"))
    monkeypatch.setattr(practice_cuda, "LocalEngine", engine)
    monkeypatch.setattr(
        practice,
        "load_exercise",
        lambda _path: (
            "https://example.test",
            {
                "problem": {"evaluator": "python-function-v1"},
            },
            "private source",
        ),
    )
    assert practice.main(command(tmp_path, tmp_path / "exercise")) == 2
    engine.assert_not_called()
    assert "CUDA exercises only" in capsys.readouterr().err


def test_platform_errors_do_not_print_private_diagnostics(tmp_path, exercise, monkeypatch, capsys):
    monkeypatch.setattr(
        practice_cuda, "compile_source", Mock(side_effect=SandboxUnavailable("PRIVATE SOURCE"))
    )
    assert practice.main(command(tmp_path, exercise[0])) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "PRIVATE SOURCE" not in captured.err
    assert "recover-compiler" in captured.err


def test_recovery_needs_no_package_image_or_network(tmp_path, monkeypatch, capsys):
    recovered = {"status": "recovered", "executed": False, "formal_result": False, "ready": False}
    method = Mock(return_value=recovered)
    monkeypatch.setattr(practice_cuda, "recover_compiler", method)
    assert (
        practice.main(
            [
                "recover-compiler",
                "--state",
                str(tmp_path / "state"),
                "--docker-socket",
                "/unused/socket",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == recovered


@pytest.mark.parametrize("architecture", ["auto", "8", "8.60", "08.6", "0.6", "８.６"])
def test_target_architecture_is_explicit(architecture):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        practice.compute_architecture(architecture)
