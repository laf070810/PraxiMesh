"""Explicit compiler image and journal binding; not a GPU evaluation backend."""

import hashlib
from pathlib import Path

from judge.container_journal import PersistentRuntimeService
from judge.container_runtime_service import SOURCES as RUNTIME_SOURCES
from judge.container_service import PREFIX

ROOT = Path(__file__).resolve().parents[1]
SOURCES = tuple(
    dict.fromkeys(
        (
            *RUNTIME_SOURCES,
            "judge/gpu_runtime.py",
            "judge/container_compiler.py",
            "judge/container_compile_protocol.py",
            "judge/container_compile_entry.py",
            "judge/container_compile_guest.py",
            "scripts/container_runtime_acceptance.py",
            "scripts/build_container_compiler.py",
            "deploy/container-compiler.fragment",
        )
    )
)


def compiler_digest():
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


class CompilerContainerService(PersistentRuntimeService):
    def required_image_labels(self):
        return {**super().required_image_labels(), PREFIX + "runtime": compiler_digest()}
