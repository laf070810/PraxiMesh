"""Prepared PostgreSQL application roles cannot initialize or broaden the installation."""

import pytest

from core import postgres_selfhost, selfhost
from core.preview import PreviewError

INSTANCE = "compose123"
GENERATION = "123456abcdef"
GPU = "GPU-fea41d22-b8d3-56e0-ff2e-40c29738bae2"


def database_url(role="web", *, generation=GENERATION, host="postgres", user=None):
    return f"postgresql://{user or f'px_{INSTANCE}_{role}'}:private@{host}:5432/px_{INSTANCE}_{generation}"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    for key in tuple(postgres_selfhost.os.environ):
        if key.startswith(("PRAXIMESH_", "COACHING_", "JUDGE_", "DB_", "APP_")):
            monkeypatch.delenv(key)
    for key, value in {
        "PRAXIMESH_COMPOSE_INSTANCE": INSTANCE,
        "PRAXIMESH_COMPOSE_GENERATION": GENERATION,
        "PRAXIMESH_COMPOSE_POSTGRES": "postgres",
        "APP_SECRET_KEY": "private-installation-secret-key-123456",
        "JUDGE_WASI_SHA256": "a" * 64,
        "DATABASE_URL": database_url(),
        "SQLITE_PATH": str(tmp_path / "unrelated.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(selfhost, "DATA", tmp_path / "not-created")
    monkeypatch.setattr(selfhost, "AI_DATA", tmp_path / "not-created-ai")
    return tmp_path


@pytest.mark.parametrize("role", ["web", "worker", "health"])
def test_prepared_role_only_configures_existing_installation(installation, monkeypatch, role):
    url = database_url("web" if role == "web" else "worker")
    monkeypatch.setenv("DATABASE_URL", url)
    configured = postgres_selfhost.environment(role)
    assert configured["DATABASE_URL"] == url
    assert "SQLITE_PATH" not in configured
    assert configured["JUDGE_WASI_SHA256"] == "a" * 64
    assert configured["JUDGE_BACKEND"] == "wasi"
    assert list(installation.iterdir()) == []


@pytest.mark.parametrize(
    "url",
    [
        database_url(user="owner"),
        database_url("worker"),
        database_url(generation="abcdef123456"),
        database_url(host="other-postgres"),
        database_url() + "?sslmode=disable",
        database_url().replace(":5432/", ":5433/"),
        "",
    ],
)
def test_foreign_or_broader_database_identity_is_rejected(installation, monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError):
        postgres_selfhost.environment("web")
    assert list(installation.iterdir()) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("PRAXIMESH_MODE", "production"),
        ("PRAXIMESH_COMPOSE_INSTANCE", ""),
        ("PRAXIMESH_COMPOSE_GENERATION", "newest"),
        ("PRAXIMESH_COMPOSE_POSTGRES", "host/other"),
        ("APP_SECRET_KEY", "REPLACE" + "x" * 40),
        ("APP_SECRET_KEY", ""),
        ("JUDGE_WASI_SHA256", "not-a-digest"),
        ("DB_SSLMODE", "require"),
        ("JUDGE_GPU_DEVICES", GPU + "," + GPU),
    ],
)
def test_unprepared_or_incompatible_settings_fail_without_writes(
    installation, monkeypatch, key, value
):
    monkeypatch.setenv(key, value)
    with pytest.raises(PreviewError):
        postgres_selfhost.environment("web")
    assert list(installation.iterdir()) == []


@pytest.mark.parametrize("role", ["worker", "health"])
def test_cpu_role_excludes_ai_credentials_and_gpu_configuration(installation, monkeypatch, role):
    monkeypatch.setenv("DATABASE_URL", database_url("worker"))
    monkeypatch.setenv("COACHING_TOKEN", "private-provider-key")
    monkeypatch.setenv("COACHING_ENDPOINT", "https://private.example/v1/chat/completions")
    monkeypatch.setenv("PRAXIMESH_LOCAL_AI", "1")
    monkeypatch.setenv("JUDGE_GPU_DEVICES", GPU)
    monkeypatch.setenv("JUDGE_GPU_RECEIPT", "/private/receipt")
    configured = postgres_selfhost.environment(role)
    assert {k: v for k, v in configured.items() if k.startswith("COACHING_")} == {
        "COACHING_ENABLED": "0"
    }
    assert {k: v for k, v in configured.items() if k.startswith("JUDGE_GPU_")} == {
        "JUDGE_GPU_DEVICES": ""
    }
    assert list(installation.iterdir()) == []


def test_web_uses_the_existing_private_local_tutor_credentials(installation, monkeypatch):
    ai = installation / "ai"
    ai.mkdir(mode=0o700)
    monkeypatch.setattr(selfhost, "AI_DATA", ai)
    monkeypatch.setenv("PRAXIMESH_LOCAL_AI", "1")
    monkeypatch.setenv("JUDGE_GPU_DEVICES", GPU)
    configured = postgres_selfhost.environment("web")
    assert configured["JUDGE_GPU_DEVICES"] == GPU
    assert configured["COACHING_ENABLED"] == "1"
    assert configured["COACHING_TOKEN"] == selfhost.secret_key(ai, "")
    assert (ai / "preview-secret.key").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in installation.iterdir()) == ["ai"]


def test_entry_has_no_management_or_migration_role(installation):
    with pytest.raises(PreviewError):
        postgres_selfhost.environment("manage")
    assert list(installation.iterdir()) == []
