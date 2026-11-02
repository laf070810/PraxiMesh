"""Public CLI boundaries; device and container execution are explicit models."""

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from judge.gpu_device_lock import DeviceLockError
from judge.sandbox import SandboxUnavailable
from scripts import practice, practice_gpu
from tests.test_practice_cuda import exercise as exercise


def command(tmp_path, operation, directory=None):
    result = [operation]
    if directory is not None:
        result.append(str(directory))
    result += [
        "--gpu-bundle",
        str(tmp_path / "images.json"),
        "--state",
        str(tmp_path / "state"),
        "--docker-socket",
        "/unused/socket",
    ]
    if operation == "prepare-gpu":
        result += ["--gpu", "GPU-12345678-1234-1234-1234-123456789abc"]
    return result


@pytest.mark.parametrize(
    "verdict,code", [("accepted", 0), ("wrong_answer", 1), ("platform_error", 2)]
)
def test_public_command_validates_package_and_keeps_local_scope(
    tmp_path, exercise, monkeypatch, capsys, verdict, code
):
    directory, package = exercise
    run = Mock(
        return_value={"result": {"verdict": verdict}, "ready": False, "formal_result": False}
    )
    monkeypatch.setattr(practice_gpu, "run_local", run)
    assert practice.main(command(tmp_path, "test-gpu", directory)) == code
    result = json.loads(capsys.readouterr().out)
    assert result["version_id"] == package["version_id"] and result["formal_result"] is False
    options = run.call_args.kwargs
    assert options["source"] == (directory / "solution.cu").read_text()
    assert options["problem"] == practice.gpu_payload(package)
    assert all(case["public"] is True for case in options["problem"]["tests"])


@pytest.mark.parametrize("field", ["image", "driver", "docker_socket", "state", "command"])
def test_downloaded_package_never_selects_infrastructure(
    tmp_path, exercise, monkeypatch, capsys, field
):
    directory, package = exercise
    package["problem"][field] = "PRIVATE SOURCE"
    (directory / "exercise.json").write_text(
        json.dumps({"server": "https://example.test", "package": package})
    )
    run = Mock(side_effect=AssertionError("must not touch Docker or GPU"))
    monkeypatch.setattr(practice_gpu, "run_local", run)
    assert practice.main(command(tmp_path, "test-gpu", directory)) == 2
    run.assert_not_called()
    assert "PRIVATE SOURCE" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [SandboxUnavailable, OSError, ValueError, DeviceLockError, InterruptedError]
)
def test_cli_failure_preserves_state_and_sanitizes_diagnostics(
    tmp_path, exercise, monkeypatch, capsys, error
):
    directory, _ = exercise
    (tmp_path / "state").mkdir()
    marker = tmp_path / "state/original"
    marker.write_text("retained")
    monkeypatch.setattr(practice_gpu, "run_local", Mock(side_effect=error("PRIVATE SOURCE")))
    assert practice.main(command(tmp_path, "test-gpu", directory)) == 2
    result = capsys.readouterr()
    assert not result.out and "PRIVATE SOURCE" not in result.err
    assert "state" in result.err and marker.read_text() == "retained"


@pytest.mark.parametrize(
    "operation,mode",
    [("initialize-gpu", "initialize"), ("recover-gpu", "recover"), ("test-gpu", "run")],
)
def test_original_state_dispatch_and_maintenance_needs_no_task_image(
    tmp_path, monkeypatch, operation, mode
):
    bundle = {"controller_image": "control", "task_image": "task", "driver_manifest": {"driver": 1}}
    args = SimpleNamespace(
        command=operation,
        gpu_bundle=tmp_path / "images.json",
        state=tmp_path / "state",
        docker_socket="socket",
    )
    engine, host = object(), object()
    monkeypatch.setattr(practice_gpu, "read", lambda path: bundle)
    monkeypatch.setattr(practice_gpu, "LocalEngine", lambda socket: engine)
    images = Mock()
    workspace = Mock(return_value=nullcontext(host))
    invocation = Mock(return_value={"ready": False})
    monkeypatch.setattr(practice_gpu, "verify_images", images)
    monkeypatch.setattr(practice_gpu, "HostWorkspace", workspace)
    monkeypatch.setattr(practice_gpu, "invoke", invocation)
    assert practice_gpu.run_local(args, problem={"public": 1}, source="opaque") == {"ready": False}
    images.assert_called_once_with(engine, bundle, task=mode == "run")
    workspace.assert_called_once_with(args.state, engine, initialize=mode == "initialize")
    assert invocation.call_args.args == (host, "control", mode)
    options = invocation.call_args.kwargs
    if mode == "run":
        assert options["source"] == "opaque" and options["image"] == "task"
    else:
        assert not set(options) & {"source", "problem", "image", "driver"}


def test_prepare_is_explicit_and_does_not_initialize_a_second_namespace(tmp_path, monkeypatch):
    args = SimpleNamespace(
        command="prepare-gpu",
        gpu_bundle=tmp_path / "images.json",
        state=tmp_path / "state",
        docker_socket="socket",
        gpu="full UUID",
    )
    engine = object()
    bundle = {"management_manifest": {"management": 1}}
    monkeypatch.setattr(practice_gpu, "read", lambda path: bundle)
    monkeypatch.setattr(practice_gpu, "LocalEngine", lambda socket: engine)
    monkeypatch.setattr(practice_gpu, "verify_images", Mock())
    prepare = Mock()
    monkeypatch.setattr(practice_gpu, "initialize_state", prepare)
    monkeypatch.setattr(practice_gpu, "HostWorkspace", Mock(side_effect=AssertionError))
    result = practice_gpu.run_local(args)
    assert result["status"] == "prepared" and result["ready"] is False
    prepare.assert_called_once_with(args.state, engine, args.gpu, bundle["management_manifest"])


@pytest.mark.parametrize("operation", ["prepare-gpu", "initialize-gpu", "recover-gpu", "test-gpu"])
def test_real_script_invocation_rejects_missing_input_without_import_traceback(tmp_path, operation):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(practice.__file__).resolve()),
            *command(
                tmp_path, operation, tmp_path / "missing" if operation == "test-gpu" else None
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2 and not result.stdout
    assert "Traceback" not in result.stderr and "Practice:" in result.stderr
