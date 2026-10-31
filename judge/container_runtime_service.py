"""Explicit trusted runsc container profile; not enabled in GPU admission."""

import hashlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from judge.container_service import PREFIX, ContainerService

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_SHA256 = "6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6"
ADDED_SYSCALLS = ("clone", "unshare", "mount", "umount2", "pivot_root", "setns", "chroot")
SOURCES = (
    "judge/__init__.py",
    "judge/container_contract.py",
    "judge/container_entry.py",
    "judge/container_service.py",
    "judge/container_runtime_service.py",
    "judge/container_oci.py",
    "judge/container_transport.py",
    "judge/container_bridge.py",
    "judge/container_connection.py",
    "judge/gpu_artifacts.py",
    "judge/gpu_command.py",
    "judge/gpu_contract.py",
    "judge/gpu_policy.py",
    "judge/gpu_session.py",
    "judge/gpu_transport.py",
    "judge/results.py",
    "judge/sandbox.py",
    "scripts/container_runtime_fixture.py",
    "scripts/container_transport_fixture.py",
    "scripts/gpu_runtime_cpu_guest.py",
    "deploy/container-runtime.Dockerfile",
    "vendor/moby/seccomp-default.json",
    "vendor/gvisor/LICENSE",
)


def seccomp_profile():
    data = (ROOT / "vendor/moby/seccomp-default.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Review the changed upstream seccomp profile before use")
    profile = json.loads(data)
    profile["syscalls"].append({"names": list(ADDED_SYSCALLS), "action": "SCMP_ACT_ALLOW"})
    return profile


def contract_digest():
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


class RuntimeContainerService(ContainerService):
    SCOPE = "praximesh-trusted-runsc-container-v1"

    def required_image_labels(self):
        return {**super().required_image_labels(), PREFIX + "runtime": contract_digest()}

    def valid_security_options(self, options):
        if type(options) is not list or len(options) != 2 or options[0] != "no-new-privileges":
            return False
        if type(options[1]) is not str or not options[1].startswith("seccomp="):
            return False
        try:
            return json.loads(options[1][8:]) == seccomp_profile()
        except (ValueError, TypeError):
            return False

    @contextmanager
    def creation_options(self):
        with tempfile.TemporaryDirectory(prefix="px-runsc-profile-") as directory:
            path = Path(directory) / "seccomp.json"
            path.write_text(json.dumps(seccomp_profile(), separators=(",", ":")))
            yield ("--security-opt=seccomp=" + str(path),)
