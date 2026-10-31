"""Compilation-only boundaries; actual NVRTC is exercised by the runsc acceptance."""

import copy
import ctypes
import json
import uuid

import pytest

from judge import gpu_runtime
from judge.container_compile_protocol import (
    request,
    validate_compiled,
    validate_identity,
    validate_request,
)
from judge.container_compiler import CompilerContainerService, compiler_digest
from judge.container_connection import ContainerConnection
from judge.container_service import PREFIX
from judge.gpu_probes import VECTOR_REFERENCE
from judge.sandbox import SandboxUnavailable
from scripts import build_container_compiler
from scripts.container_runtime_acceptance import build_context
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_supervision import IMAGE, snapshot


@pytest.mark.parametrize(
    "target", [None, [], [8], [True, 6], [8, False], [0, 6], [8, 10], [100, 0], "86"]
)
def test_invalid_architecture_never_loads_a_native_library(monkeypatch, target):
    monkeypatch.setattr(gpu_runtime.c, "CDLL", lambda *_a: pytest.fail("Native library loaded"))
    with pytest.raises(gpu_runtime.DriverFailure) as error:
        gpu_runtime.compile_ptx(VECTOR_REFERENCE, "vector-add-f32-v1", target)
    assert error.value.exit_code == 70


@pytest.mark.parametrize(
    "source,operation",
    [("x" * 20001, "vector-add-f32-v1"), ([], "vector-add-f32-v1"), (VECTOR_REFERENCE, "host")],
)
def test_invalid_source_or_operation_never_loads_a_native_library(monkeypatch, source, operation):
    monkeypatch.setattr(gpu_runtime.c, "CDLL", lambda *_a: pytest.fail("Native library loaded"))
    with pytest.raises(ValueError):
        gpu_runtime.compile_ptx(source, operation, [8, 6])


def test_formal_kernel_delegates_to_the_same_compiler(monkeypatch):
    kernel = gpu_runtime.Kernel.__new__(gpu_runtime.Kernel)
    kernel.operation, kernel.arch, kernel.nvrtc = "vector-add-f32-v1", (8, 6), object()
    output = ctypes.create_string_buffer(b"fixed PTX")
    calls = []

    def compile(source, operation, architecture, *, library):
        calls.append((source, operation, architecture, library))
        return output

    monkeypatch.setattr(gpu_runtime, "compile_ptx", compile)
    assert kernel._compile(VECTOR_REFERENCE) is output
    assert calls == [(VECTOR_REFERENCE, kernel.operation, kernel.arch, kernel.nvrtc)]


@pytest.mark.parametrize("defect", ["extra", "protocol", "target", "source"])
def test_compiler_request_cannot_add_verdicts_or_alter_protocol(defect):
    value = request(VECTOR_REFERENCE, "vector-add-f32-v1", [8, 6])
    if defect == "extra":
        value["verdict"] = "accepted"
    elif defect == "protocol":
        value["protocol"] = 1
    elif defect == "target":
        value["architecture"] = [8.0, 6]
    else:
        value["source"] = None
    with pytest.raises(ValueError):
        validate_request(value)


@pytest.mark.parametrize(
    "defect", ["gpu", "driver", "numeric-bool", "version", "floating-version", "extra"]
)
def test_device_free_identity_is_exact(defect):
    value = {
        "event": "compiler-ready",
        "nvrtc_version": [13, 1],
        "gpu_nodes": [],
        "cuda_driver_available": False,
    }
    validate_identity(value)
    if defect == "gpu":
        value["gpu_nodes"] = ["/dev/nvidia0"]
    elif defect == "driver":
        value["cuda_driver_available"] = True
    elif defect == "numeric-bool":
        value["cuda_driver_available"] = 0
    elif defect == "version":
        value["nvrtc_version"] = [13, 2]
    elif defect == "floating-version":
        value["nvrtc_version"] = [13.0, 1]
    else:
        value["verdict"] = "accepted"
    with pytest.raises(SandboxUnavailable):
        validate_identity(value)


@pytest.mark.parametrize("defect", ["ptx", "verdict", "architecture", "size", "bool-size", "hash"])
def test_success_is_bounded_metadata_not_executable_or_verdict(defect):
    value = {"event": "compiled", "architecture": [8, 6], "ptx_bytes": 1024, "ptx_sha256": "a" * 64}
    validate_compiled(value, [8, 6])
    if defect == "ptx":
        value["ptx"] = "arbitrary output"
    elif defect == "verdict":
        value["verdict"] = "accepted"
    elif defect == "architecture":
        value["architecture"] = [8.0, 6]
    elif defect == "size":
        value["ptx_bytes"] = 4 * 1024**2 + 1
    elif defect == "bool-size":
        value["ptx_bytes"] = True
    else:
        value["ptx_sha256"] = "not a digest"
    with pytest.raises(SandboxUnavailable):
        validate_compiled(value, [8, 6])


def test_new_compiler_image_retains_frozen_journal_cleanup(journal):  # noqa: F811
    service = CompilerContainerService(journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE)
    service.image_env = []
    state = snapshot(service)
    state["Config"]["Labels"].update(service.required_image_labels())
    service.record_intent(state["Config"]["Cmd"])
    original = copy.deepcopy(journal.load())
    assert original["labels"][PREFIX + "runtime"] == compiler_digest()
    state["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(original["security"][1]),
    ]
    journal.engine.state = state
    result = journal.recover()
    assert result["removed"] is True and result["ready"] is False
    assert journal.load() is None
    assert (
        json.loads((journal.directory / result["history"]).read_text())["labels"]
        == original["labels"]
    )


def test_unknown_compiler_archive_never_reaches_docker(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    archive = tmp_path / "untrusted.tar.xz"
    archive.write_bytes(b"not the pinned NVRTC archive")
    monkeypatch.setattr(build_container_compiler, "stage_context", lambda *_a: context)
    monkeypatch.setattr(
        build_container_compiler,
        "build_context",
        lambda *_a, **_kw: pytest.fail("Docker build was reached"),
    )
    with pytest.raises(ValueError, match="NVRTC archive"):
        build_container_compiler.build_image(None, None, archive, tmp_path, "unused")
    assert not (context / "compiler/nvrtc").exists()
    assert not (context / "compiler/harness").exists()


def test_source_change_during_staging_stops_before_build(tmp_path, monkeypatch):
    import scripts.container_runtime_acceptance as builder

    monkeypatch.setattr(builder.subprocess, "run", lambda *_a, **_kw: pytest.fail("Build started"))
    with pytest.raises(RuntimeError, match="during staging"):
        build_context(None, tmp_path, tmp_path, "unused", lambda: "new", expected_digest="old")


def test_explicit_compiler_service_reaches_original_live_instance_check(journal, monkeypatch):  # noqa: F811
    service = CompilerContainerService(journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE)
    monkeypatch.setattr(service, "inspect", lambda: None)
    with pytest.raises(SandboxUnavailable, match="original live"):
        ContainerConnection(service, {}, on_tick=lambda: None)


def test_arbitrary_compiler_subclass_cannot_bypass_connection_checks(journal):  # noqa: F811
    class Unreviewed(CompilerContainerService):
        pass

    service = Unreviewed(journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE)
    with pytest.raises(ValueError, match="verified runtime"):
        ContainerConnection(service, {}, on_tick=lambda: None)
