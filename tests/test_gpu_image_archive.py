"""Offline images must retain the pinned manifest and every referenced blob."""

import hashlib
import io
import json
import tarfile

import pytest

from scripts.gpu_kernel_probe import verify_image_archive


def descriptor(data):
    return {"digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}


def archive_fixture(tmp_path, monkeypatch, defect=None):
    config = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
    layer = b"payload"
    manifest = json.dumps(
        {"schemaVersion": 2, "config": descriptor(config), "layers": [descriptor(layer)]}
    ).encode()
    pinned = descriptor(manifest)
    monkeypatch.setattr("judge.gpu_artifacts.IMAGE", "example/image@" + pinned["digest"])
    files = {
        "oci-layout": b'{"imageLayoutVersion": "1.0.0"}',
        "index.json": json.dumps({"schemaVersion": 2, "manifests": [pinned]}).encode(),
    }
    for data in (manifest, config, layer):
        files["blobs/" + descriptor(data)["digest"].replace(":", "/")] = data
    layer_path = "blobs/" + descriptor(layer)["digest"].replace(":", "/")
    if defect == "tampered":
        files[layer_path] = b"forged!"
    elif defect == "resized":
        files[layer_path] += b"x"
    elif defect == "unreferenced":
        files["blobs/sha256/" + hashlib.sha256(b"extra").hexdigest()] = b"extra"
    elif defect == "different_image":
        monkeypatch.setattr("judge.gpu_artifacts.IMAGE", "example/image@sha256:" + "0" * 64)
    archive = tmp_path / "image.oci.tar"
    with tarfile.open(archive, "w") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        if defect == "duplicate":
            info = tarfile.TarInfo("index.json")
            info.size = len(files["index.json"])
            bundle.addfile(info, io.BytesIO(files["index.json"]))
        elif defect == "link":
            info = tarfile.TarInfo("blobs/sha256/" + "0" * 64)
            info.type = tarfile.SYMTYPE
            info.linkname = "/unrelated-host-file"
            bundle.addfile(info)
    return archive, pinned["digest"], descriptor(config)["digest"]


def test_complete_pinned_graph_can_be_validated_without_extracting(tmp_path, monkeypatch):
    archive, manifest, config = archive_fixture(tmp_path, monkeypatch)
    identity = verify_image_archive(archive)
    assert identity["image_manifest"] == manifest
    assert identity["image_id"] == config
    assert sorted(path.name for path in tmp_path.iterdir()) == [archive.name]


@pytest.mark.parametrize(
    "defect", ["tampered", "resized", "unreferenced", "different_image", "duplicate", "link"]
)
def test_substituted_incomplete_or_ambiguous_archives_fail_closed(tmp_path, monkeypatch, defect):
    archive, _, _ = archive_fixture(tmp_path, monkeypatch, defect)
    with pytest.raises(ValueError):
        verify_image_archive(archive)


def test_oversized_archive_is_rejected_before_parsing(tmp_path):
    archive = tmp_path / "large.tar"
    with archive.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        verify_image_archive(archive)
