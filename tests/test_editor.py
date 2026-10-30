import hashlib
import json
import re

from django.conf import settings


def test_editor_policy_nonce_is_scoped_and_changes_between_responses(catalog, client):
    first = client.get("/problems/sum-positive/")
    second = client.get("/problems/sum-positive/")
    nonce = re.search(r'data-nonce="([^"]+)"', first.content.decode())[1]
    assert len(nonce) >= 24
    assert f"'nonce-{nonce}'" in first["Content-Security-Policy"]
    assert nonce not in second.content.decode()
    assert "worker-src 'self'" in first["Content-Security-Policy"]
    assert "'wasm-unsafe-eval'" in first["Content-Security-Policy"]
    assert "'unsafe-eval'" not in first["Content-Security-Policy"]
    assert "'unsafe-inline'" not in first["Content-Security-Policy"]
    for url in ("/", "/practice/", "/accounts/login/", "/problems/not-a-problem/"):
        response = client.get(url)
        assert "wasm-unsafe-eval" not in response["Content-Security-Policy"]
        assert "nonce-" not in response["Content-Security-Policy"]


def test_committed_editor_assets_match_sources_and_release_size_budget():
    root = settings.BASE_DIR
    assets = root / "static/editor"
    manifest = json.loads((assets / "manifest.json").read_text())
    for name, digest in manifest["inputs"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    for name, expected in manifest["outputs"].items():
        contents = (assets / name).read_bytes()
        assert hashlib.sha256(contents).hexdigest() == expected["sha256"], name
        assert len(contents) == expected["bytes"]
        assert len(contents) < 10 * 1024 * 1024
    assert (assets / "ruff.wasm").read_bytes().startswith(b"\x00asm")
    assert (assets / "clang-format.wasm").read_bytes().startswith(b"\x00asm")
