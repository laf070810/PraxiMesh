"""Configuration boundaries for full Compose; execution is tested in real containers."""

import pytest

from core import selfhost
from core.preview import PreviewError


@pytest.fixture
def instance(tmp_path, monkeypatch):
    for key in ("APP_SECRET_KEY", "DATABASE_URL", "PRAXIMESH_LOCAL_AI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PRAXIMESH_MODE", "local")
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    identity = tmp_path / "wasi.sha256"
    identity.write_text("a" * 64 + "\n")
    ai = tmp_path / "ai"
    ai.mkdir(mode=0o700)
    monkeypatch.setattr(selfhost, "DATA", data)
    monkeypatch.setattr(selfhost, "IDENTITY", identity)
    monkeypatch.setattr(selfhost, "AI_DATA", ai)
    return data, identity, ai


def test_worker_cannot_initialize_database_or_key(instance):
    data, _, _ = instance
    with pytest.raises(PreviewError):
        selfhost.environment("worker")
    assert list(data.iterdir()) == []


def test_worker_uses_packaged_identity_and_excludes_provider_credentials(instance, monkeypatch):
    data, _, _ = instance
    database = data / "praximesh.sqlite3"
    database.write_bytes(b"existing database")
    database.chmod(0o600)
    key = selfhost.secret_key(data, "")
    monkeypatch.setenv("JUDGE_WASI_SHA256", "b" * 64)
    monkeypatch.setenv("COACHING_TOKEN", "private-model-token")
    monkeypatch.setenv("COACHING_ENDPOINT", "https://private.example/v1/chat/completions")
    result = selfhost.environment("worker")
    assert result["JUDGE_WASI_SHA256"] == "a" * 64
    assert result["APP_SECRET_KEY"] == key
    assert result["JUDGE_ENABLED"] == "1"
    assert {key: value for key, value in result.items() if key.startswith("COACHING_")} == {
        "COACHING_ENABLED": "0"
    }
    assert database.read_bytes() == b"existing database"


@pytest.mark.parametrize("digest", ["", "a" * 63, "g" * 64, "a" * 64 + " extra"])
def test_invalid_artifact_identity_fails_before_data_changes(instance, digest):
    data, identity, _ = instance
    identity.write_text(digest)
    with pytest.raises(PreviewError):
        selfhost.environment("web")
    assert list(data.iterdir()) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("DATABASE_URL", "postgresql://example/db"),
        ("PRAXIMESH_MODE", "production"),
    ],
)
def test_single_host_bootstrap_cannot_replace_production(instance, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(PreviewError):
        selfhost.environment("web")


def test_local_model_token_is_private_persistent_and_separate_from_database(instance, monkeypatch):
    data, _, ai = instance
    monkeypatch.setenv("PRAXIMESH_LOCAL_AI", "1")
    first = selfhost.environment("web")
    second = selfhost.environment("web")
    assert first["COACHING_TOKEN"] == second["COACHING_TOKEN"]
    assert len(first["COACHING_TOKEN"]) >= 50
    assert first["COACHING_ALLOW_LOOPBACK_HTTP"] == "1"
    assert first["COACHING_ENDPOINT"] == "http://127.0.0.1:8081/v1/chat/completions"
    assert (ai / "preview-secret.key").stat().st_mode & 0o777 == 0o600
    assert not list(data.iterdir())


def test_remote_model_cannot_use_the_local_http_exception(instance, monkeypatch):
    monkeypatch.setenv("COACHING_ALLOW_LOOPBACK_HTTP", "1")
    assert selfhost.environment("web")["COACHING_ALLOW_LOOPBACK_HTTP"] == "0"
