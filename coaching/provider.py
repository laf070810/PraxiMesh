"""Bounded text-only model transport; no tools, evaluation or implicit credentials."""

import ipaddress
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_PROMPT_BYTES = 24000
MAX_INSTRUCTION_BYTES = 4000
MAX_WIRE_BYTES = 40000
MAX_RESPONSE_BYTES = 32768
MAX_TEXT_CHARS = 4000


class ProviderError(ValueError):
    """A fixed, safe code; never include endpoint credentials or remote output."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str
    model: str
    token: str = field(repr=False)
    allow_loopback_http: bool = False
    ca_file: str | None = None
    ca_pem: str | None = None
    timeout_seconds: int = 20
    max_tokens: int = 192
    disable_thinking: bool = False

    def validate(self):
        if not isinstance(self.endpoint, str):
            raise ProviderError("invalid_configuration")
        try:
            endpoint = urllib.parse.urlsplit(self.endpoint)
            port = endpoint.port
        except (ValueError, TypeError):
            raise ProviderError("invalid_configuration") from None
        if (
            not isinstance(self.endpoint, str)
            or not self.endpoint.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in self.endpoint)
            or endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path != "/v1/chat/completions"
            or (port is not None and not 1 <= port <= 65535)
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 20
            or type(self.max_tokens) is not int
            or not 16 <= self.max_tokens <= 256
            or type(self.allow_loopback_http) is not bool
            or type(self.disable_thinking) is not bool
            or not isinstance(self.model, str)
            or not 1 <= len(self.model) <= 160
            or any(ord(char) < 32 or ord(char) == 127 for char in self.model)
            or not isinstance(self.token, str)
            or not 1 <= len(self.token) <= 2048
            or any(not 33 <= ord(char) <= 126 for char in self.token)
            or (self.ca_file is not None and not isinstance(self.ca_file, str))
            or (
                self.ca_pem is not None
                and (
                    not isinstance(self.ca_pem, str)
                    or not self.ca_pem.isascii()
                    or not 1 <= len(self.ca_pem) <= 8192
                    or "\x00" in self.ca_pem
                    or self.ca_file is not None
                )
            )
        ):
            raise ProviderError("invalid_configuration")
        if endpoint.scheme == "http":
            try:
                local = ipaddress.ip_address(endpoint.hostname).is_loopback
            except ValueError:
                local = False
            if not self.allow_loopback_http or not local:
                raise ProviderError("invalid_configuration")
        try:
            self.model.encode("utf-8")
        except UnicodeError:
            raise ProviderError("invalid_configuration") from None


@dataclass(frozen=True)
class HintResult:
    text: str
    model: str
    truncated: bool
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: int


def _text_bytes(value, limit):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProviderError("invalid_request")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ProviderError("invalid_request") from None
    if size > limit:
        raise ProviderError("invalid_request")


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        pending = [(value, 0)]
        seen = 0
        while pending:
            item, depth = pending.pop()
            seen += 1
            if depth > 16 or seen > 4096:
                raise ValueError("structure")
            if isinstance(item, dict):
                pending.extend((part, depth + 1) for pair in item.items() for part in pair)
            elif isinstance(item, list):
                pending.extend((part, depth + 1) for part in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("finite")
            elif isinstance(item, str):
                item.encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_response") from None
    return value


def _parse_response(raw, config, elapsed_ms):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ProviderError("invalid_response")
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise ProviderError("invalid_response")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("invalid_response")
    choice = choices[0]
    message = choice.get("message")
    reason = choice.get("finish_reason")
    if (
        not isinstance(message, dict)
        or message.get("role") != "assistant"
        or message.get("tool_calls") not in (None, [])
        or message.get("function_call") is not None
        or not isinstance(reason, str)
        or reason not in {"stop", "length"}
    ):
        raise ProviderError("invalid_response")
    text = message.get("content")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_TEXT_CHARS
        or "\x00" in text
    ):
        raise ProviderError("invalid_response")
    usage = value.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ProviderError("invalid_response")
    tokens = []
    for key, limit in (("prompt_tokens", 100000), ("completion_tokens", config.max_tokens)):
        count = usage.get(key) if usage else None
        if count is not None and (type(count) is not int or not 0 <= count <= limit):
            raise ProviderError("invalid_response")
        tokens.append(count)
    # The configured, operator-selected identity is authoritative; arbitrary
    # response metadata is not copied into a learner-visible record.
    return HintResult(text.strip(), config.model, reason == "length", *tokens, elapsed_ms)


def request_hint(config, *, instructions, prompt):
    config.validate()
    _text_bytes(instructions, MAX_INSTRUCTION_BYTES)
    _text_bytes(prompt, MAX_PROMPT_BYTES)
    wire = json.dumps(
        {"config": asdict(config), "instructions": instructions, "prompt": prompt},
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(wire) > MAX_WIRE_BYTES:
        raise ProviderError("invalid_request")
    if sys.platform != "linux":
        raise ProviderError("unsupported_platform")
    begin = time.monotonic()
    # Only this fixed, reviewed program executes. Prompts and credentials travel
    # on stdin; neither the command line nor inherited environment contains them.
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).with_name("provider_driver.py")),
                str(os.getpid()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise ProviderError("provider_unavailable") from None
    try:
        output, _ = child.communicate(wire, timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        raise ProviderError("timeout") from None
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=3)
    if child.returncode != 0 or len(output) > MAX_RESPONSE_BYTES * 6 + 128:
        raise ProviderError("provider_unavailable")
    envelope = _strict_json(output)
    if not isinstance(envelope, dict):
        raise ProviderError("invalid_response")
    if set(envelope) == {"error"}:
        code = envelope["error"]
        if not isinstance(code, str) or code not in {
            "provider_unavailable",
            "http_error",
            "response_too_large",
            "timeout",
        }:
            code = "provider_unavailable"
        raise ProviderError(code)
    if set(envelope) != {"body"}:
        raise ProviderError("invalid_response")
    return _parse_response(envelope["body"], config, int((time.monotonic() - begin) * 1000))
