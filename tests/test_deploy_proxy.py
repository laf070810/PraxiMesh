"""The proxy controller must prove ownership and separate intent from live state."""

import copy
import errno
import json
import os
import re
import socket
import stat
from pathlib import Path

import pytest

from scripts import deploy_proxy
from scripts.deploy_proxy import DeploymentProxy, ProxyError


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    executable = tmp_path / "caddy"
    executable.write_text("opaque test executable; never executed\n")
    executable.chmod(0o700)
    proxy = DeploymentProxy(
        {"caddy": str(executable), "origin": "http://127.0.0.1:8123"}, tmp_path / "proxy"
    )

    def adapted(source, *, validate=False):
        match = re.search(r"reverse_proxy 127\.0\.0\.1:(\d+)", source)
        handlers = (
            [{"handler": "reverse_proxy", "upstreams": [{"dial": f"127.0.0.1:{match[1]}"}]}]
            if match
            else [{"handler": "static_response", "status_code": 503}]
        )
        return {
            "admin": {"listen": proxy.admin_address},
            "apps": {
                "http": {
                    "servers": {
                        proxy.server_name: {
                            "listen": ["127.0.0.1:8123"],
                            "routes": [{"match": [{"host": ["127.0.0.1"]}], "handle": handlers}],
                        }
                    }
                }
            },
            # The fixture preserves every generated token for exact comparison.
            "fixture_source": source,
        }

    monkeypatch.setattr(proxy, "_adapt", adapted)
    proxy.prepare()
    return proxy


def serve_config(proxy, monkeypatch, actual=None, *, reload_error=False, ignore_reload=False):
    state = {"actual": actual or proxy._adapt(proxy._render()), "posts": [], "reads": 0}

    def request(method, path, body=None, headers=None):
        assert path == "/config/"
        if method == "GET":
            state["reads"] += 1
            return json.dumps(state["actual"]).encode(), {"etag": '"/config/ fixture"'}
        assert method == "POST"
        assert headers["If-Match"] == '"/config/ fixture"'
        candidate = json.loads(body)
        # Persistence must precede the live change, including when reload fails.
        assert proxy.config_path.read_text() == candidate["fixture_source"]
        state["posts"].append(candidate)
        if reload_error:
            raise ProxyError("Caddy admin request failed with HTTP 412")
        if not ignore_reload:
            state["actual"] = candidate
        return b"", {}

    monkeypatch.setattr(proxy, "_request", request)
    return state


@pytest.mark.parametrize(
    "origin,bind",
    [
        ("http://example.com", "127.0.0.1"),
        ("http://localhost:8123", "0.0.0.0"),
        ("http://127.0.0.1:8123", "192.0.2.1"),
        ("http://127.0.0.1.example.com", "127.0.0.1"),
        ("http://localhost.evil", "127.0.0.1"),
        ("https://user:password@example.com", "127.0.0.1"),
        ("https://example.com/", "127.0.0.1"),
        ("https://example.com?x=1", "127.0.0.1"),
        ("https://example.com#fragment", "127.0.0.1"),
        ("https://example.com\nrespond secret", "127.0.0.1"),
        ("https://{$SECRET}", "127.0.0.1"),
        ("ftp://localhost:8123", "127.0.0.1"),
        ("http://localhost:0", "127.0.0.1"),
        ("http://localhost:", "127.0.0.1"),
        ("http://localhost:65536", "127.0.0.1"),
        ("http://localhost:8123", "localhost"),
    ],
)
def test_rejects_unsafe_or_malformed_origins(proxy, origin, bind):
    with pytest.raises(ProxyError):
        DeploymentProxy(
            {"caddy": str(proxy.caddy), "origin": origin, "bind": bind}, proxy.state_dir
        )


@pytest.mark.parametrize(
    "origin,bind",
    [
        ("http://127.0.0.2:8123", "127.0.0.1"),
        ("http://localhost:8123", "127.0.0.1"),
        ("http://[::1]:8123", "::1"),
        ("https://example.com", "0.0.0.0"),
    ],
)
def test_accepts_explicit_https_or_loopback_http(proxy, origin, bind):
    other = DeploymentProxy(
        {"caddy": str(proxy.caddy), "origin": origin, "bind": bind}, proxy.state_dir
    )
    assert other.origin == origin


@pytest.mark.parametrize(
    "replacement", ["relative", "/tmp/{$PRIVATE}", "/tmp/a\nrespond secret", "/tmp/a/../b"]
)
def test_rejects_injectable_or_nonnormalized_paths(proxy, replacement):
    with pytest.raises(ProxyError):
        DeploymentProxy({"caddy": replacement, "origin": proxy.origin}, proxy.state_dir)


def test_rejects_symlinked_binary_or_directory(proxy, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(proxy.caddy)
    with pytest.raises(ProxyError, match="symlink"):
        DeploymentProxy({"caddy": str(alias), "origin": proxy.origin}, proxy.state_dir)
    alias.unlink()
    alias.symlink_to(proxy.state_dir, target_is_directory=True)
    with pytest.raises(ProxyError, match="symlink"):
        DeploymentProxy({"caddy": str(proxy.caddy), "origin": proxy.origin}, alias / "child")


def test_rejects_public_state_directory(proxy):
    proxy.state_dir.chmod(0o755)
    with pytest.raises(ProxyError, match="0700"):
        proxy.prepare()


def test_prepare_is_private_idempotent_and_preserves_restart_activation(proxy):
    assert stat.S_IMODE(proxy.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(proxy.config_path.stat().st_mode) == 0o600
    proxy._persist(proxy._render(8124))
    proxy.prepare()
    assert "reverse_proxy 127.0.0.1:8124" in proxy.config_path.read_text()
    assert proxy.command() == [
        str(proxy.caddy),
        "run",
        "--config",
        str(proxy.config_path),
        "--adapter",
        "caddyfile",
    ]
    assert "--resume" not in proxy.command()
    assert proxy.environment()["XDG_DATA_HOME"].startswith(str(proxy.state_dir) + "/")
    assert "HOME" not in proxy.environment()


def test_restart_symlink_is_never_overwritten(proxy, tmp_path):
    original = tmp_path / "untouched"
    original.write_text("private unrelated state")
    proxy.config_path.unlink()
    proxy.config_path.symlink_to(original)
    with pytest.raises(ProxyError, match="symlink"):
        proxy.prepare()
    assert original.read_text() == "private unrelated state"


def test_caddy_storage_cannot_escape_private_directory(proxy, tmp_path):
    (proxy.state_dir / "data").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ProxyError, match="symlink"):
        proxy.prepare()


def test_existing_endpoint_prevents_starting_replacement_process(proxy):
    proxy.admin_socket.write_text("unknown endpoint")
    with pytest.raises(ProxyError, match="refuse to replace"):
        proxy.command()
    with pytest.raises(ProxyError, match="Unix socket"):
        proxy.prepare()


def test_ensure_directory_does_not_create_or_mutate_restart_configuration(proxy):
    proxy.config_path.unlink()
    assert proxy.ensure_directory() == proxy.state_dir
    assert not proxy.config_path.exists()


def test_owned_stale_socket_is_removed_before_restart(proxy):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(proxy.admin_socket))
        # No listener or service is started. The kernel must report ECONNREFUSED.
        assert proxy.command()[1] == "run"
        assert not proxy.admin_socket.exists()


def test_live_connection_is_not_replaced(proxy, monkeypatch):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(proxy.admin_socket))
        monkeypatch.setattr(deploy_proxy._UnixHTTP, "connect", lambda self: None)
        with pytest.raises(ProxyError, match="running proxy"):
            proxy.command()
        assert proxy.admin_socket.exists()


def test_socket_connection_error_other_than_refusal_is_not_staleness(proxy, monkeypatch):
    def unavailable(self):
        raise OSError(errno.EACCES, "unavailable")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(proxy.admin_socket))
        monkeypatch.setattr(deploy_proxy._UnixHTTP, "connect", unavailable)
        with pytest.raises(ProxyError, match="cannot establish"):
            proxy.command()
        assert proxy.admin_socket.exists()


def test_replaced_socket_is_not_unlinked_after_connection_refusal(proxy, monkeypatch):
    def replaced(self):
        proxy.admin_socket.unlink()
        proxy.admin_socket.write_text("changed during inspection")
        raise ConnectionRefusedError(errno.ECONNREFUSED, "stale")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.bind(str(proxy.admin_socket))
        monkeypatch.setattr(deploy_proxy._UnixHTTP, "connect", replaced)
        with pytest.raises(ProxyError, match="changed while checking"):
            proxy.command()
        assert proxy.admin_socket.read_text() == "changed during inspection"


def test_restart_must_match_exact_generated_configuration(proxy):
    proxy.config_path.write_text(proxy._render() + "import /tmp/external\n")
    with pytest.raises(ProxyError, match="exact owned"):
        proxy.command()


def test_transition_confirms_live_state_after_durable_intent(proxy, monkeypatch):
    state = serve_config(proxy, monkeypatch)
    assert proxy.probe()["mode"] == "maintenance"
    result = proxy.activate(8124)
    assert result["mode"] == "active"
    assert result["upstream"] == "127.0.0.1:8124"
    assert state["reads"] == 3
    assert proxy.maintenance()["mode"] == "maintenance"
    assert len(state["posts"]) == 2


def test_failed_reload_preserves_intended_restart_and_reports_actual_old_state(proxy, monkeypatch):
    state = serve_config(proxy, monkeypatch, reload_error=True)
    with pytest.raises(ProxyError, match="412"):
        proxy.activate(8124)
    assert proxy.config_path.read_text() == proxy._render(8124)
    assert proxy.status()["mode"] == "maintenance"
    assert len(state["posts"]) == 1


def test_successful_reload_response_alone_is_not_confirmation(proxy, monkeypatch):
    serve_config(proxy, monkeypatch, ignore_reload=True)
    with pytest.raises(ProxyError, match="did not confirm"):
        proxy.activate(8124)


@pytest.mark.parametrize("mutation", ["admin", "server", "listen", "site", "extra", "upstream"])
def test_refuses_to_reload_unowned_or_modified_live_configuration(proxy, monkeypatch, mutation):
    actual = copy.deepcopy(proxy._adapt(proxy._render(8124)))
    server = actual["apps"]["http"]["servers"][proxy.server_name]
    if mutation == "admin":
        actual["admin"]["listen"] = "localhost:2019"
    elif mutation == "server":
        actual["apps"]["http"]["servers"]["other"] = {}
    elif mutation == "listen":
        server["listen"] = ["0.0.0.0:8123"]
    elif mutation == "site":
        server["routes"][0]["match"] = [{"host": ["other.example"]}]
    elif mutation == "extra":
        server["routes"].append({"handle": [{"handler": "file_server"}]})
    else:
        server["routes"][0]["handle"][0]["upstreams"][0]["dial"] = "192.0.2.1:8124"
    state = serve_config(proxy, monkeypatch, actual)
    original = proxy.config_path.read_bytes()
    with pytest.raises(ProxyError, match="owned configuration"):
        proxy.activate(8125)
    assert not state["posts"]
    assert proxy.config_path.read_bytes() == original


def test_missing_admin_socket_cannot_be_replaced_by_restart_file(proxy):
    with pytest.raises(ProxyError, match="Unix admin socket"):
        proxy.status()


def test_admin_socket_symlink_and_regular_file_rejected(proxy, tmp_path):
    proxy.admin_socket.write_text("not a socket")
    with pytest.raises(ProxyError, match="Unix socket"):
        proxy.status()
    proxy.admin_socket.unlink()
    proxy.admin_socket.symlink_to(tmp_path / "missing")
    with pytest.raises(ProxyError, match="symlink"):
        proxy.status()


@pytest.mark.parametrize("port", [True, "8124", 0, -1, 65536, 8124.0])
def test_upstream_port_is_not_interpolated_from_untrusted_text(proxy, port):
    with pytest.raises(ProxyError, match="upstream port"):
        proxy.activate(port)


def test_caddy_validation_failure_leaves_restart_file_unchanged(proxy, monkeypatch):
    original = proxy.config_path.read_bytes()

    def invalid(*args, **kwargs):
        raise ProxyError("Caddy configuration validation failed")

    monkeypatch.setattr(proxy, "_adapt", invalid)
    with pytest.raises(ProxyError, match="validation failed"):
        proxy.activate(8124)
    assert proxy.config_path.read_bytes() == original


def test_tls_files_must_be_paired_and_safe(proxy, tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("test fixture, not a usable certificate")
    with pytest.raises(ProxyError, match="paired"):
        DeploymentProxy(
            {"caddy": str(proxy.caddy), "origin": "https://localhost:8123", "tls_cert": str(cert)},
            proxy.state_dir,
        )
    with pytest.raises(ProxyError, match="require HTTPS"):
        DeploymentProxy(
            {
                "caddy": str(proxy.caddy),
                "origin": proxy.origin,
                "tls_cert": str(cert),
                "tls_key": str(cert),
            },
            proxy.state_dir,
        )


def test_real_pinned_caddy_adapts_exact_proxy_and_maintenance_without_starting(tmp_path):
    caddy = Path(__file__).resolve().parents[1] / "var/tools/caddy-2.11.7"
    if not caddy.is_file():
        pytest.skip("pinned Caddy executable has not been downloaded")
    # Keep the Unix path short even on verbose pytest temporary directory names.
    proxy = DeploymentProxy(
        {"caddy": str(caddy), "origin": "http://127.0.0.1:8123"}, tmp_path / "p"
    )
    proxy.prepare()
    assert not proxy.admin_socket.exists()
    actual = proxy._adapt(proxy._render(8124), validate=True)
    assert actual["admin"]["listen"] == proxy.admin_address
    server = actual["apps"]["http"]["servers"][proxy.server_name]
    assert server["listen"] == ["127.0.0.1:8123"]
    serialized = json.dumps(actual)
    assert "127.0.0.1:8124" in serialized
    assert '"X-Forwarded-Proto": ["{http.request.scheme}"]' in serialized
    assert '"X-Real-Ip": ["{http.request.remote.host}"]' in serialized
    assert '"max_size": 100000' in serialized
    assert actual["admin"]["config"]["persist"] is False
    assert os.stat(proxy.config_path).st_mode & 0o777 == 0o600
