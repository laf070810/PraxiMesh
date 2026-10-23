"""Transport/validation tests use an HTTP double, never claim real AI inference."""

import json
import ssl
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from coaching.provider import ProviderConfig, ProviderError, _parse_response, request_hint


def response(text="Check the empty input and the sign of each value.", reason="stop"):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": reason}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 12},
    }


@pytest.fixture
def provider_server():
    servers = []

    def start(*, status=200, payload=None, delay=False, tls=None):
        seen = []
        data = json.dumps(response()).encode() if payload is None else payload

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append({"unexpected_get": self.path})
                self.send_response(500)
                self.end_headers()

            def do_POST(self):
                seen.append(
                    {
                        "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", "/unexpected-redirect/")
                self.end_headers()
                try:
                    if delay:
                        for _ in range(20):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.15)
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        scheme = "https" if tls else "http"
        config = ProviderConfig(
            f"{scheme}://127.0.0.1:{server.server_port}/v1/chat/completions",
            "test-model",
            "private-test-token",
            allow_loopback_http=not tls,
        )
        return config, seen

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_transport_preserves_data_and_has_no_tools_or_proxy(provider_server, monkeypatch):
    config, seen = provider_server()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    result = request_hint(
        replace(config, disable_thinking=True), instructions="Give a hint.", prompt="公开题意"
    )
    assert result.text == response()["choices"][0]["message"]["content"]
    assert result.model == "test-model" and not result.truncated
    assert result.input_tokens == 20 and result.output_tokens == 12
    assert seen == [
        {
            "authorization": "Bearer private-test-token",
            "body": {
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Give a hint."},
                    {"role": "user", "content": "公开题意"},
                ],
                "max_tokens": 192,
                "temperature": 0.3,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        }
    ]
    assert "private-test-token" not in repr(config)


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_are_bounded_and_do_not_follow_redirects(provider_server, status):
    config, seen = provider_server(status=status)
    with pytest.raises(ProviderError, match="^http_error$"):
        request_hint(config, instructions="Hint only", prompt="Question")
    assert len(seen) == 1 and "unexpected_get" not in seen[0]


def test_actual_child_timeout_reaps_process_under_trickled_response(provider_server, monkeypatch):
    import coaching.provider as module

    children = []
    real_popen = module.subprocess.Popen

    def track(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", track)
    config, _ = provider_server(delay=True)
    begin = time.monotonic()
    with pytest.raises(ProviderError, match="^timeout$"):
        request_hint(replace(config, timeout_seconds=1), instructions="Hint", prompt="Question")
    assert time.monotonic() - begin < 3
    assert len(children) == 1 and children[0].poll() is not None


def test_response_byte_limit_is_enforced_before_parsing(provider_server):
    config, _ = provider_server(payload=b"x" * 32769)
    with pytest.raises(ProviderError, match="^response_too_large$"):
        request_hint(config, instructions="Hint", prompt="Question")


def test_tls_rejects_unknown_ca_and_accepts_explicit_private_ca(provider_server, tmp_path):
    from scripts.production_acceptance import pki

    cert, key, _, _ = pki(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    config, seen = provider_server(tls=tls)
    with pytest.raises(ProviderError, match="^provider_unavailable$"):
        request_hint(config, instructions="Hint", prompt="Question")
    assert seen == []
    result = request_hint(
        replace(config, ca_file=str(tmp_path / "ca.pem")), instructions="Hint", prompt="Question"
    )
    assert result.text and len(seen) == 1
    embedded = request_hint(
        replace(config, ca_pem=(tmp_path / "ca.pem").read_text()),
        instructions="Hint",
        prompt="Question",
    )
    assert embedded.text and len(seen) == 2


@pytest.mark.parametrize("pem", [[], "", "x" * 8193, "\u2603", "a\x00b"])
def test_private_ca_pem_is_bounded_and_text_only(pem):
    config = ProviderConfig("https://example.org/v1/chat/completions", "model", "token", ca_pem=pem)
    with pytest.raises(ProviderError, match="^invalid_configuration$"):
        config.validate()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost/v1/chat/completions",
        "http://example.org/v1/chat/completions",
        "http://169.254.169.254/v1/chat/completions",
        "https://user:secret@example.org/v1/chat/completions",
        "https://example.org/v1/chat/completions?secret=1",
        "https://example.org/v1/chat/completions#fragment",
        "https://example.org/another-path",
        "https://example.org:0/v1/chat/completions",
        "https://example.org:99999/v1/chat/completions",
        "https://example.org\n/v1/chat/completions",
        {"endpoint": "https://example.org/v1/chat/completions"},
    ],
)
def test_configuration_is_not_an_arbitrary_fetch_interface(endpoint):
    config = ProviderConfig(endpoint, "model", "token", allow_loopback_http=True)
    with pytest.raises(ProviderError, match="^invalid_configuration$"):
        request_hint(config, instructions="Hint", prompt="Question")


@pytest.mark.parametrize("prompt", ["", "\ud800", "x\x00y", "x" * 24001])
def test_invalid_input_never_starts_a_request(provider_server, prompt):
    config, seen = provider_server()
    with pytest.raises(ProviderError, match="^invalid_request$"):
        request_hint(config, instructions="Hint", prompt=prompt)
    assert not seen


@pytest.mark.parametrize(
    "raw",
    [
        '{"choices":[],"choices":[]}',
        '{"number":1e999}',
        '{"number":NaN}',
        '{"x":"\\ud800"}',
        "[" * 17 + "0" + "]" * 17,
        json.dumps(response(text="x" * 4001)),
        json.dumps(response(text="")),
        json.dumps(response(reason="tool_calls")),
        json.dumps(response(reason=[])),
        json.dumps(response(reason={})),
        json.dumps(response(reason=None)),
        json.dumps(response(reason=1)),
        json.dumps({**response(), "usage": {"completion_tokens": True}}),
        json.dumps({**response(), "usage": {"completion_tokens": 257}}),
        json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"role": "user", "content": "x"}}]}
        ),
    ],
)
def test_response_contract_rejects_forgery_and_ambiguous_json(raw):
    config = ProviderConfig("https://example.org/v1/chat/completions", "model", "token")
    with pytest.raises(ProviderError, match="^invalid_response$"):
        _parse_response(raw, config, 5)


def test_truncation_is_explicit_and_missing_usage_is_unknown():
    config = ProviderConfig("https://example.org/v1/chat/completions", "model", "token")
    data = response(reason="length")
    del data["usage"]
    result = _parse_response(json.dumps(data), config, 20)
    assert result.truncated and result.input_tokens is None and result.output_tokens is None
