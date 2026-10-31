"""Driver provenance and refusal boundaries; hardware is never mocked as ready."""

import copy
import hashlib
import io
import json
import os
import struct
import uuid
from types import SimpleNamespace

import pytest

from judge import gpu_runtime
from judge.container_driver import DriverContainerService, image_contract
from judge.container_service import PREFIX
from judge.driver_artifacts import (
    PROFILE,
    file_identity,
    library_names,
    manifest_digest,
    prepare_request,
    validate_manifest,
    verify_bundle,
    version_from_kernel,
)
from scripts import build_container_driver as builder
from scripts.container_driver_acceptance import preserve_journal
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_supervision import IMAGE, snapshot


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "driver"
    directory.mkdir()
    value = {"schema": 1, "profile": PROFILE, "driver_version": "590.48.01", "libraries": {}}
    for name, soname in library_names(value["driver_version"]).items():
        data = ("synthetic boundary fixture " + name).encode()
        (directory / name).write_bytes(data)
        value["libraries"][name] = {
            "soname": soname,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if name != soname:
            (directory / soname).symlink_to(name)
    (directory / "manifest.json").write_text(json.dumps(value))
    return directory, value


@pytest.mark.parametrize(
    "defect",
    [
        "extra",
        "schema-bool",
        "profile",
        "traversal",
        "missing",
        "extra-library",
        "soname",
        "size-bool",
        "oversize",
        "hash",
    ],
)
def test_manifest_rejects_incomplete_or_reinterpreted_identity(bundle, defect):
    _, value = bundle
    first = next(iter(value["libraries"].values()))
    if defect == "extra":
        value["ready"] = True
    elif defect == "schema-bool":
        value["schema"] = True
    elif defect == "profile":
        value["profile"] = "other"
    elif defect == "traversal":
        value["driver_version"] = "../../lib"
    elif defect == "missing":
        value["libraries"].popitem()
    elif defect == "extra-library":
        value["libraries"]["unexpected.so"] = first
    elif defect == "soname":
        first["soname"] = "libcuda.so.99"
    elif defect == "size-bool":
        first["size"] = True
    elif defect == "oversize":
        first["size"] = 256 * 1024**2 + 1
    else:
        first["sha256"] = "g" * 64
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize(
    "defect", ["bytes", "size", "symlink", "fifo", "alias", "extra", "manifest"]
)
def test_bundle_refuses_changes_before_native_loading(bundle, defect):
    directory, value = bundle
    first = directory / next(iter(value["libraries"]))
    verify_bundle(directory, value)
    if defect == "bytes":
        first.write_bytes(b"X" * first.stat().st_size)
    elif defect == "size":
        first.write_bytes(b"short")
    elif defect in {"symlink", "fifo"}:
        first.unlink()
        if defect == "symlink":
            first.symlink_to("/etc/passwd")
        else:
            os.mkfifo(first)
    elif defect == "alias":
        alias = directory / "libcuda.so.1"
        alias.unlink()
        alias.symlink_to("/tmp/foreign-library")
    elif defect == "extra":
        (directory / "foreign.so").touch()
    else:
        changed = copy.deepcopy(value)
        next(iter(changed["libraries"].values()))["sha256"] = "0" * 64
        (directory / "manifest.json").write_text(json.dumps(changed))
    with pytest.raises((ValueError, OSError)):
        verify_bundle(directory, value)


def test_manifest_is_canonical_and_new_image_cannot_reuse_old_identity(bundle):
    _, value = bundle
    assert manifest_digest(value) == manifest_digest(json.loads(json.dumps(value, sort_keys=True)))
    original = image_contract(value)
    next(iter(value["libraries"].values()))["sha256"] = "0" * 64
    assert image_contract(value) != original


def test_driver_prepare_uses_the_actual_guest_wire_protocol(bundle, monkeypatch):
    _, manifest = bundle
    request = prepare_request(manifest)
    monkeypatch.setattr(
        gpu_runtime.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode() + b"\n")),
    )
    decoded = gpu_runtime.message()
    assert decoded["protocol"] == 1 and decoded["operation"] == "prepare-driver"
    assert decoded["manifest"] == manifest
    manifest["libraries"].clear()
    assert len(request["manifest"]["libraries"]) == 4


def test_driver_identity_retains_existing_journal_recovery(bundle, journal):  # noqa: F811
    _, value = bundle
    service = DriverContainerService(
        journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE, driver_manifest=value
    )
    labels = service.required_image_labels()
    assert set(labels) == {PREFIX + "protocol", PREFIX + "runtime"}
    next(iter(value["libraries"].values()))["sha256"] = "0" * 64
    assert service.required_image_labels() == labels
    service.image_env = []
    state = snapshot(service)
    state["Config"]["Labels"].update(labels)
    service.record_intent(state["Config"]["Cmd"])
    record = journal.load()
    state["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(record["security"][1]),
    ]
    journal.engine.state = state
    assert journal.recover()["removed"] is True
    assert journal.load() is None


def test_kernel_identity_requires_open_driver_and_exact_version():
    text = (
        "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  590.48.01  Release Build\n"
        "GCC version: gcc 11.5.0\n"
    )
    assert version_from_kernel(text) == "590.48.01"
    for invalid in (
        text.replace("Open ", ""),
        text.replace("x86_64", "aarch64"),
        text.replace("590.48.01", "590.48"),
        text + "extra\n",
    ):
        with pytest.raises(ValueError):
            version_from_kernel(invalid)


def test_snapshot_never_follows_links_or_blocks_on_fifo(tmp_path):
    linked = tmp_path / "link"
    linked.symlink_to("/etc/passwd")
    with pytest.raises(OSError):
        builder.stable_copy(linked, tmp_path / "copy", 1024)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        builder.stable_copy(fifo, tmp_path / "fifo-copy", 1024)


def test_snapshot_bounds_and_unprivileged_driver_rejection(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"fixed artifact")
    identity = builder.stable_copy(source, tmp_path / "copy", 1024)
    assert identity["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        builder.stable_copy(source, tmp_path / "bounded", 1)
    source.chmod(0o666)
    with pytest.raises(ValueError):
        builder.stable_copy(source, tmp_path / "untrusted", 1024, trusted=True)
    with pytest.raises(ValueError):
        builder.trusted_directory(tmp_path)


def test_snapshot_ignores_only_access_time(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"original")
    before = path.stat()
    altered = SimpleNamespace(
        **{
            name: getattr(before, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
    )
    altered.st_atime_ns = before.st_atime_ns + 1
    assert file_identity(altered) == file_identity(before)
    altered.st_ctime_ns += 1
    assert file_identity(altered) != file_identity(before)


def test_static_elf_inspection_does_not_execute_the_library(tmp_path, monkeypatch):
    path = tmp_path / "library"
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 3, 62)
    path.write_bytes(header)
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b" 0x0 (SONAME) Library soname: [libcuda.so.1]\n", stderr=b"")

    monkeypatch.setattr(builder.subprocess, "run", inspect)
    assert builder.elf_soname(path) == "libcuda.so.1"
    assert calls[0][0] == ["/usr/bin/readelf", "--dynamic", "--wide", str(path)]
    path.write_bytes(b"not an ELF library")
    with pytest.raises(ValueError):
        builder.elf_soname(path)
    assert len(calls) == 1


def test_installed_snapshot_is_rechecked_when_kernel_changes(bundle, tmp_path, monkeypatch):
    directory, value = bundle
    version = tmp_path / "version"
    version.write_text(
        "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  595.01.01  Release\nGCC version: gcc\n"
    )
    monkeypatch.setattr(builder, "KERNEL_VERSION", version)
    monkeypatch.setattr(builder, "trusted_directory", lambda path: directory)
    with pytest.raises(ValueError, match="Kernel driver no longer"):
        builder.verify_installed(directory, value)


@pytest.mark.parametrize("state", ["empty", "pending", "unreadable"])
def test_acceptance_keeps_storage_when_pending_state_cannot_be_excluded(tmp_path, state):
    source, output = tmp_path / "journal", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "original.json").write_text("original ownership")

    def load():
        if state == "unreadable":
            raise OSError("Cannot verify original record")
        return None if state == "empty" else {"pending": True}

    report = {}
    record_store = SimpleNamespace(directory=source, load=load)
    if state == "unreadable":
        with pytest.raises(OSError):
            preserve_journal(record_store, output, report)
    else:
        preserve_journal(record_store, output, report)
    assert report.get("retain_storage", False) is (state != "empty")
    assert (source / "original.json").read_text() == "original ownership"
    assert (output / "journal/original.json").read_text() == "original ownership"
