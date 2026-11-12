"""Image source permissions must not inherit the operator's private umask."""

import os
import stat

import pytest

from judge import container_gpu
from scripts import build_container_compiler, build_container_driver, container_runtime_acceptance


@pytest.mark.parametrize("mask", [0o077, 0o000])
@pytest.mark.parametrize("kind", ["runtime", "compiler", "driver", "gpu-task"])
def test_staged_sources_are_readable_inside_private_build_directory(
    tmp_path, monkeypatch, mask, kind
):
    output = tmp_path / "private-build"
    output.mkdir(mode=0o700)
    runtime, nvrtc = tmp_path / "runtime.tar.bz2", tmp_path / "nvrtc.tar.xz"
    runtime.write_bytes(b"runtime archive fixture")
    nvrtc.write_bytes(b"compiler archive fixture")
    source = container_runtime_acceptance.ROOT / "judge/container_entry.py"
    original_source = source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
    # Only artifact extraction and Docker are replaced; source staging is real.
    monkeypatch.setattr(
        container_runtime_acceptance,
        "runtime_from_archive",
        lambda _archive, destination: destination.mkdir(mode=0o700),
    )
    for builder in (build_container_compiler, build_container_driver):
        monkeypatch.setattr(builder, "prepare_kernel_artifacts", lambda *_args: {})
        monkeypatch.setattr(builder, "build_context", lambda *_args, **_kwargs: "fixture-image")
    monkeypatch.setattr(build_container_driver, "snapshot_driver", lambda *_args: {})
    monkeypatch.setattr(build_container_driver, "verify_bundle", lambda *_args: None)
    monkeypatch.setattr(build_container_driver, "verify_installed", lambda *_args: None)
    monkeypatch.setattr(build_container_driver, "image_contract", lambda *_args: "driver-fixture")
    monkeypatch.setattr(container_gpu, "image_contract", lambda *_args: "gpu-fixture")

    original_mask = os.umask(mask)
    try:
        if kind == "runtime":
            container_runtime_acceptance.stage_context(runtime, output)
        elif kind == "compiler":
            build_container_compiler.build_image(None, runtime, nvrtc, output, "unused")
        else:
            build_container_driver.build_image(
                None, runtime, nvrtc, tmp_path, output, "unused", device_tasks=kind == "gpu-task"
            )
    finally:
        observed_mask = os.umask(original_mask)
    assert observed_mask == mask
    context = output / "context"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(context.stat().st_mode) == 0o700
    if kind != "runtime":
        assert stat.S_IMODE((context / "compiler").stat().st_mode) == 0o700
    assert (source.read_bytes(), stat.S_IMODE(source.stat().st_mode)) == original_source
    assert (context / "container_entry.py").read_bytes() == original_source[0]

    required = {"container_entry.py", "judge/container_contract.py", "container_kernel_fixture.py"}
    if kind == "compiler":
        required |= {"container_compile_entry.py", "container_compile_guest.py"}
    if kind in {"driver", "gpu-task"}:
        required |= {
            "driver_artifacts.py",
            "container_driver_guest.py",
            "container_driver_fixture.py",
        }
        assert stat.S_IMODE((context / "driver-cases.json").stat().st_mode) == 0o644
    if kind == "gpu-task":
        required |= {
            "container_kernel_entry.py",
            "container_kernel_guest.py",
            "container_gpu_guest.py",
        }
        assert stat.S_IMODE((context / "device-mounts/nvidia0").stat().st_mode) == 0o444
    staged = {path.relative_to(context).as_posix(): path for path in context.rglob("*.py")}
    assert required <= staged.keys()
    for name, path in staged.items():
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, name
        for parent in path.parents:
            if parent == context:
                break
            assert stat.S_IMODE(parent.stat().st_mode) == 0o755, name
