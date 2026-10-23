"""Provider credentials stay within the configured Web generation."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy import Deployment
from scripts.deploy_coaching import coaching_environment


@pytest.fixture(autouse=True)
def no_proxy_process(monkeypatch):
    # These tests exercise configuration/environment projection, not Caddy.
    monkeypatch.setattr("scripts.deploy.DeploymentProxy", lambda *args: None)


@pytest.fixture
def provider():
    return {
        "endpoint": "https://tutor.example.org/v1/chat/completions",
        "model": "operator-model",
        "token": "synthetic-provider-token",
        "description": "Synthetic provider; no live request in this test.",
        "daily_limit": 5,
        "concurrency": 1,
        "max_tokens": 192,
        "disable_thinking": False,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "http://127.0.0.1/v1/chat/completions"),
        ("endpoint", "https://user:secret@example.org/v1/chat/completions"),
        ("token", "secret\nInjected: value"),
        ("description", ""),
        ("daily_limit", True),
        ("concurrency", 5),
        ("max_tokens", 1000),
        ("disable_thinking", "false"),
        ("ca_pem", "not a certificate"),
        ("unexpected", "value"),
    ],
)
def test_rejects_unsafe_or_ambiguous_provider_configuration(provider, field, value):
    provider[field] = value
    with pytest.raises(ValueError, match="^Invalid optional HTTPS coaching configuration$"):
        coaching_environment(provider)


def test_only_web_receives_provider_configuration(provider, tmp_path):
    config = {
        "proxy": {"origin": "https://learn.example.org"},
        "application": {
            "secret_key": "web-secret",
            "worker_secret": "worker-secret",
            "registration_open": False,
            "coaching": provider,
        },
    }
    deployment = Deployment(config, tmp_path)
    deployment.state = {"site_id": "synthetic"}
    deployment._database = SimpleNamespace(url=lambda database, role: "synthetic-" + role)
    deployment.networks = lambda: {"frontend": {"IPAM": {"Config": [{"Gateway": "172.20.0.1"}]}}}
    generation = {"database": {}, "manifest": {"judge": {"bundle_sha256": "a" * 64}}}
    web = deployment.environment(generation, "web")
    assert web["COACHING_TOKEN"] == provider["token"]
    assert web["COACHING_ENDPOINT"] == provider["endpoint"]
    for role in ("owner", "worker"):
        assert not any(
            key.startswith("COACHING_") for key in deployment.environment(generation, role)
        )
    del config["application"]["coaching"]
    assert not any(key.startswith("COACHING_") for key in deployment.environment(generation, "web"))


def test_private_coordinator_accepts_provider_and_pins_it(tmp_path, provider):
    # Reuse the existing fixture's exact configuration contract through the real validator.
    from scripts.deploy import configuration

    config = json.loads((Path(__file__).parents[1] / "deploy/coordinator.json.example").read_text())
    config["database"].update(
        owner_password="a" * 32, web_password="b" * 32, worker_password="c" * 32
    )
    config["application"].update(secret_key="d" * 50, worker_secret="e" * 50, coaching=provider)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    assert configuration(path) == config
    before = Deployment(config, tmp_path / "first").config_digest
    changed = copy.deepcopy(config)
    changed["application"]["coaching"]["token"] = "rotated-synthetic-token"
    assert Deployment(changed, tmp_path / "second").config_digest != before
