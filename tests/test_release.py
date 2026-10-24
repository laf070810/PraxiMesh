"""Artifact-reader rejection tests. Fixtures are opaque and are never executed."""

import hashlib
import io
import json
import tarfile

import pytest

from judge.wasi_policy import TARGET
from scripts.release import (
    SOURCE_FILES,
    ReleaseError,
    identity,
    initial_content_commands,
    sha256,
    snapshot,
    source_archive,
    verify,
)


def test_initial_content_commands_respect_frozen_release_capabilities():
    assert initial_content_commands({"source_files": {}}) == [["content_import"]]
    files = {
        "content/en.json": "digest",
        "content/projects.json": "digest",
        "localization/management/commands/translation_import.py": "digest",
        "projects/management/commands/project_import.py": "digest",
    }
    assert initial_content_commands({"source_files": files}) == [
        ["content_import", "--translations", "/app/content/en.json"],
        ["project_import", "/app/content/projects.json"],
    ]
    files.pop("projects/management/commands/project_import.py")
    with pytest.raises(ReleaseError):
        initial_content_commands({"source_files": files})


def write_manifest(root, manifest):
    (root / "release.json").write_text(json.dumps(manifest))
    return sha256(root / "release.json")


def test_challenge_demo_requires_reviewed_importer_and_preserves_old_release_commands():
    files = {"content/challenges.json": "digest"}
    with pytest.raises(ReleaseError):
        initial_content_commands({"source_files": files})
    files["challenges/management/commands/challenge_import.py"] = "digest"
    assert initial_content_commands({"source_files": files}) == [
        ["content_import"],
        ["challenge_import", "/app/content/challenges.json"],
    ]


def refresh_payload(root, manifest, name):
    manifest["payloads"][name] = {
        "bytes": (root / name).stat().st_size,
        "sha256": sha256(root / name),
    }
    return write_manifest(root, manifest)


@pytest.fixture(params=[False, True], ids=["legacy", "with-development-guide"])
def release(tmp_path, request):
    root, source = tmp_path / "release", tmp_path / "source"
    root.mkdir()
    source.mkdir()
    files = {}
    for name in SOURCE_FILES | ({"AGENTS.md"} if request.param else set()):
        data = name.encode()
        (source / name).write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    source_archive(source, files, root / "source.tar.gz")
    images, entries = {}, []
    with tarfile.open(root / "images.tar", "w") as archive:
        for role in ["web", "worker"]:
            layer = ("opaque fixture for " + role).encode()
            member = tarfile.TarInfo(role + ".tar")
            member.size = len(layer)
            archive.addfile(member, io.BytesIO(layer))
            data = json.dumps(
                {
                    "role": role,
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()],
                    },
                }
            ).encode()
            images[role] = "sha256:" + hashlib.sha256(data).hexdigest()
            member = tarfile.TarInfo(role + ".json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            entries.append({"Config": member.name, "Layers": [role + ".tar"], "RepoTags": None})
        data = json.dumps(entries).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    manifest = {
        "format": 1,
        "platform": "linux/amd64",
        "source_id": identity(files),
        "source_files": files,
        "images": images,
        "judge": {
            "backend": "wasi",
            "target": TARGET,
            "bundle_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
        },
        "payloads": {
            name: {"sha256": sha256(root / name), "bytes": (root / name).stat().st_size}
            for name in ["source.tar.gz", "images.tar"]
        },
    }
    return root, manifest, write_manifest(root, manifest)


def test_release_requires_independently_trusted_digest(release):
    root, manifest, digest = release
    assert verify(root, digest) == manifest
    with pytest.raises(ReleaseError, match="digest"):
        verify(root, "c" * 64)


@pytest.mark.parametrize("name", ["release.json", "images.tar", "source.tar.gz"])
def test_release_rejects_mutated_bytes(release, name):
    root, _, digest = release
    with (root / name).open("ab") as file:
        file.write(b"modified")
    with pytest.raises(ReleaseError):
        verify(root, digest)


@pytest.mark.parametrize("change", ["symlink", "extra", "missing", "image", "source", "backend"])
def test_release_rejects_unsupported_or_inconsistent_artifacts(release, tmp_path, change):
    root, manifest, digest = release
    if change == "symlink":
        (root / "images.tar").rename(tmp_path / "outside.tar")
        (root / "images.tar").symlink_to(tmp_path / "outside.tar")
    elif change == "extra":
        (root / "credentials.env").write_text("test only")
    elif change == "missing":
        (root / "source.tar.gz").unlink()
    elif change == "image":
        manifest["images"]["worker"] = "sha256:" + "c" * 64
        digest = write_manifest(root, manifest)
    elif change == "source":
        manifest["source_files"]["manage.py"] = "c" * 64
        manifest["source_id"] = identity(manifest["source_files"])
        digest = write_manifest(root, manifest)
    else:
        manifest["judge"]["backend"] = "host-python"
        digest = write_manifest(root, manifest)
    with pytest.raises(ReleaseError):
        verify(root, digest)


@pytest.mark.parametrize("kind", ["traversal", "symlink", "duplicate"])
def test_release_never_extracts_unsafe_source_archive(release, kind):
    root, manifest, _ = release
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("../escaped" if kind == "traversal" else "manage.py")
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
        else:
            data = b"manage.py"
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            if kind == "duplicate":
                archive.addfile(member, io.BytesIO(data))
    digest = refresh_payload(root, manifest, "source.tar.gz")
    with pytest.raises(ReleaseError):
        verify(root, digest)
    assert not (root.parent / "escaped").exists()


def test_snapshot_omits_state_and_rejects_config_or_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in SOURCE_FILES:
        (source / name).write_text(name)
    (source / "AGENTS.md").write_text("Development guidance")
    (source / ".env").write_text("must not be copied")
    (source / "var").mkdir()
    (source / "var/secret").write_text("must not be copied")
    captured = tmp_path / "captured"
    files = snapshot(source, captured)
    assert ".env" not in files and not (captured / "var").exists()
    assert files["AGENTS.md"] == sha256(captured / "AGENTS.md")
    assert (captured / "AGENTS.md").read_text() == "Development guidance"
    (source / "core").mkdir()
    (source / "core/private.env").write_text("must fail")
    with pytest.raises(ReleaseError):
        snapshot(source, tmp_path / "refused")
    (source / "core/private.env").unlink()
    (source / "core/link.py").symlink_to(source / "manage.py")
    with pytest.raises(ReleaseError, match="symlink"):
        snapshot(source, tmp_path / "refused-link")


@pytest.mark.parametrize("target_exists", [False, True], ids=["dangling", "existing"])
def test_snapshot_rejects_optional_guide_symlinks(tmp_path, target_exists):
    source = tmp_path / "source"
    source.mkdir()
    for name in SOURCE_FILES:
        (source / name).write_text(name)
    outside = tmp_path / "outside.md"
    if target_exists:
        outside.write_text("Must not enter the release")
    (source / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ReleaseError, match="Source symlink forbidden: AGENTS.md"):
        snapshot(source, tmp_path / "captured")


@pytest.mark.parametrize("change", ["missing", "modified"])
def test_layer_validation_does_not_depend_on_cached_daemon_images(release, change):
    root, manifest, _ = release
    with tarfile.open(root / "images.tar", "r:") as archive:
        members = [(item, archive.extractfile(item).read()) for item in archive]
    with tarfile.open(root / "images.tar", "w") as archive:
        for item, data in members:
            if item.name == "worker.tar":
                if change == "missing":
                    continue
                data = b"wrong layer content"
                item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    digest = refresh_payload(root, manifest, "images.tar")
    with pytest.raises(ReleaseError):
        verify(root, digest)


def test_packaged_worker_never_starts_after_failed_probe(settings, monkeypatch):
    from django.core.management.base import CommandError

    from core.management.commands import wasi_service

    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise CommandError("probe failed")

    monkeypatch.setattr(wasi_service, "call_command", fail)
    with pytest.raises(CommandError):
        wasi_service.Command().handle(worker_id="test", once=True)
    assert calls == ["sandbox_check"]
