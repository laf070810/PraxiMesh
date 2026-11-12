"""Build a pinned, device-free NVRTC guest on the existing supervised runtime."""

import os
import stat

from judge.container_compiler import ROOT, compiler_digest
from judge.gpu_artifacts import prepare_kernel_artifacts
from scripts.container_runtime_acceptance import build_context, copy_image_source, stage_context


def build_image(engine, runtime_archive, nvrtc_archive, output, tag):
    expected = compiler_digest()
    context = stage_context(runtime_archive, output)
    compiler = context / "compiler"
    compiler.mkdir(mode=0o700)
    archive = compiler / "nvrtc.tar.xz"
    fd = os.open(nvrtc_archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source, archive.open("xb") as target:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024**2:
            raise ValueError("A bounded regular NVRTC archive is required")
        count = 0
        while data := source.read(1024 * 1024):
            count += len(data)
            if count > info.st_size:
                raise ValueError("NVRTC archive changed during staging")
            target.write(data)
        if count != info.st_size:
            raise ValueError("NVRTC archive changed during staging")
    identity = prepare_kernel_artifacts(compiler, archive, ROOT)
    archive.unlink()
    for name in (
        "container_compile_entry.py",
        "container_compile_guest.py",
        "container_compile_protocol.py",
    ):
        copy_image_source(ROOT / "judge" / name, context / name)
    with (context / "Dockerfile").open("ab") as target:
        target.write((ROOT / "deploy/container-compiler.fragment").read_bytes())
    image = build_context(engine, context, output, tag, compiler_digest, expected_digest=expected)
    return image, identity
