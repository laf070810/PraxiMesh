"""Reject configuration and ownership drift before starting the CPU runtime."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge import container_oci
from judge.container_oci import ContainerRuntime, specification
from judge.container_runtime_service import (
    RuntimeContainerService,
    contract_digest,
    seccomp_profile,
)
from judge.container_service import ContainerService
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable
from scripts.container_runtime_acceptance import build_image
from tests.test_container_supervision import snapshot


@pytest.fixture
def service(tmp_path):
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        yield RuntimeContainerService(
            guard, uuid.uuid4(), uuid.uuid4(), engine=None, image="sha256:" + "a" * 64
        )


def options():
    return ["no-new-privileges", "seccomp=" + json.dumps(seccomp_profile())]


def test_expanded_profile_is_not_accepted_by_plain_supervision(service):
    plain = ContainerService(
        service.guard, service.job_id, service.reservation_token, engine=None, image=service.image
    )
    assert plain.name != service.name
    assert service.valid_security_options(options())
    assert not plain.valid_security_options(options())
    assert not service.valid_security_options(["no-new-privileges"])
    assert plain.valid_security_options(["no-new-privileges"])


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        ["no-new-privileges", "seccomp=unconfined"],
        ["no-new-privileges", "seccomp={}"],
        ["no-new-privileges", "seccomp=[]"],
        ["seccomp={}", "no-new-privileges"],
        ["no-new-privileges", "seccomp=null"],
        ["no-new-privileges", "seccomp=bad"],
    ],
)
def test_only_the_complete_reviewed_profile_is_accepted(service, bad):
    assert not service.valid_security_options(bad)


def test_changed_permissions_and_image_scope_are_rejected(service):
    changed = seccomp_profile()
    changed["syscalls"].append({"names": ["bpf"], "action": "SCMP_ACT_ALLOW"})
    assert not service.valid_security_options(
        ["no-new-privileges", "seccomp=" + json.dumps(changed)]
    )
    assert service.required_image_labels()["io.praximesh.supervision.runtime"] == contract_digest()
    assert len(contract_digest()) == 64


def test_inherited_runtime_image_label_is_bound_to_container_identity(service, monkeypatch):
    record = snapshot(service)
    record["Config"]["Labels"].update(service.required_image_labels())
    record["HostConfig"]["SecurityOpt"] = options()
    service.image_env = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps([record]).encode()),
    )
    assert service.inspect()["running"] is True
    record["Config"]["Labels"]["io.praximesh.supervision.runtime"] = "changed"
    with pytest.raises(SandboxUnavailable, match="ownership"):
        service.inspect()


def test_profile_file_is_private_temporary_and_complete(service):
    with service.creation_options() as values:
        assert len(values) == 1
        path = Path(values[0].removeprefix("--security-opt=seccomp="))
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert json.loads(path.read_text()) == seccomp_profile()
    assert not path.exists()


def test_runtime_rejects_host_launch_before_starting_process(monkeypatch):
    monkeypatch.setattr(container_oci.os, "getuid", lambda: 2031)
    monkeypatch.setattr(
        container_oci.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Must not launch on the host"),
    )
    with pytest.raises(SandboxUnavailable, match="supervised"):
        ContainerRuntime(deadline=time.monotonic() + 20, on_tick=lambda: None)


def test_pid_one_reaper_preserves_main_exit_and_reaps_other_children():
    # A separate parent owns both fixed CPU children; never reap pytest's peers.
    program = """
import os, subprocess, sys, time
from judge.container_entry import reap_children
other = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(0)'])
main = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.15); raise SystemExit(7)'])
until = time.monotonic() + 3
while main.returncode is None and time.monotonic() < until:
    reap_children(main)
    time.sleep(.01)
assert main.returncode == 7
try:
    os.waitpid(other.pid, os.WNOHANG)
except ChildProcessError:
    pass
else:
    raise AssertionError('Orphan-style child was not reaped')
"""
    subprocess.run([sys.executable, "-c", program], check=True, timeout=5, capture_output=True)


@pytest.mark.parametrize(
    "command",
    [[], ["relative"], ["/bin/false", "\0"], ["/bin/false", None], "/bin/sh", ["/bin/false", ""]],
)
def test_invalid_trusted_command_is_rejected(command):
    with pytest.raises(ValueError):
        specification(command)


def test_specification_does_not_inherit_host_configuration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "private")
    monkeypatch.setenv("HTTP_PROXY", "private")
    spec = specification(["/usr/local/bin/python", "-I", "/opt/praximesh/cpu_guest.py"])
    assert spec["root"]["readonly"] is True
    assert spec["process"]["noNewPrivileges"] is True
    assert not any(spec["process"]["capabilities"].values())
    assert not any("private" in item for item in spec["process"]["env"])
    assert all(mount["type"] != "bind" for mount in spec["mounts"])
    assert not spec["linux"].get("cgroupsPath")
    assert set(spec["linux"]["resources"]) == {"devices"}  # No false guest cgroup limits.


@pytest.fixture
def runtime(tmp_path):
    instance = ContainerRuntime.__new__(ContainerRuntime)
    instance.base = tmp_path
    instance.deadline = time.monotonic() + 20
    instance.on_tick = lambda: None
    instance.containers = {}
    instance.environment = {}
    instance.diagnostics = False
    return instance


@pytest.mark.parametrize("change", ["arguments", "symlink", "already-started"])
def test_modified_or_reused_bundle_never_launches(runtime, monkeypatch, change):
    runtime.create("one", "/usr/local/bin/python", ["-I", "/opt/praximesh/cpu_guest.py"])
    config = runtime.base / "one/config.json"
    if change == "arguments":
        data = json.loads(config.read_text())
        data["process"]["args"] = ["/bin/sh"]
        config.write_text(json.dumps(data))
    elif change == "symlink":
        target = runtime.base / "replacement.json"
        config.rename(target)
        config.symlink_to(target)
    else:
        runtime.containers["one"]["process"] = object()
    monkeypatch.setattr(
        container_oci.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Changed bundle launched")
    )
    with pytest.raises(SandboxUnavailable, match="unchanged"):
        runtime.attach("one")


def test_expired_deadline_and_repeated_names_are_not_reused(runtime):
    runtime.create("one", "/bin/false", [])
    with pytest.raises(ValueError):
        runtime.create("one", "/bin/false", [])
    with pytest.raises(ValueError):
        runtime.create("../other", "/bin/false", [])
    runtime.deadline = time.monotonic() - 1
    with pytest.raises(SandboxUnavailable, match="deadline"):
        runtime.attach("one")


def test_mismatched_runtime_bundle_never_authorizes_delete(runtime):
    runtime.create("one", "/bin/false", [])
    calls = []

    def command(*args):
        calls.append(args)
        data = (
            [{"id": "one"}]
            if args[0] == "list"
            else {"id": "one", "bundle": "/unowned", "status": "running"}
        )
        return SimpleNamespace(stdout=json.dumps(data).encode())

    runtime.command = command
    with pytest.raises(SandboxUnavailable, match="identity"):
        runtime.stop_container("one")
    assert not any(args[0] == "delete" for args in calls)


def test_empty_runtime_list_and_unobserved_processes_are_distinct(runtime, monkeypatch):
    runtime.create("one", "/bin/false", [])
    runtime.command = lambda *_a: SimpleNamespace(stdout=b"null")
    assert runtime.list_containers() == []
    runtime.containers["one"]["before"] = {}
    runtime.deadline = time.monotonic()
    monkeypatch.setattr(container_oci, "processes", lambda: {123: 456})
    with pytest.raises(SandboxUnavailable, match="processes remain"):
        runtime.stop_container("one")
    assert not runtime.containers["one"].get("closed")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized", "wrong-digest"])
def test_untrusted_archive_never_reaches_docker_build(tmp_path, monkeypatch, kind):
    archive = tmp_path / "archive"
    if kind == "fifo":
        os.mkfifo(archive)
    elif kind == "symlink":
        archive.symlink_to(tmp_path / "absent")
    else:
        with archive.open("wb") as stream:
            if kind == "oversized":
                stream.truncate(128 * 1024**2 + 1)
            else:
                stream.write(b"untrusted archive")
    out = tmp_path / "result"
    out.mkdir()
    monkeypatch.setattr(
        "scripts.container_runtime_acceptance.subprocess.run",
        lambda *_a, **_k: pytest.fail("Unexpected build"),
    )
    with pytest.raises((OSError, ValueError)):
        build_image(None, archive, out, "unused")
