"""Private stdlib-only HTTPS helper with no environment or redirect inheritance."""

import ctypes
import json
import os
import resource
import signal
import ssl
import sys
import urllib.error
import urllib.request

MAX_REQUEST_BYTES = 40000
MAX_RESPONSE_BYTES = 32768


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def main():
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected_parent:
        return {"error": "provider_unavailable"}
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        return {"error": "provider_unavailable"}
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        return {"error": "provider_unavailable"}
    data = json.loads(raw)
    config = data["config"]
    body = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": data["instructions"]},
            {"role": "user", "content": data["prompt"]},
        ],
        "max_tokens": config["max_tokens"],
        "temperature": 0.3,
        "stream": False,
    }
    if config["disable_thinking"]:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    context = ssl.create_default_context(cafile=config["ca_file"], cadata=config["ca_pem"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    request = urllib.request.Request(
        config["endpoint"],
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + config["token"],
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=config["timeout_seconds"]) as response:
            if response.status != 200:
                return {"error": "http_error"}
            if response.headers.get_content_type() != "application/json":
                return {"error": "provider_unavailable"}
            if response.headers.get("Content-Encoding", "identity") != "identity":
                return {"error": "provider_unavailable"}
            output = response.read(MAX_RESPONSE_BYTES + 1)
            if len(output) > MAX_RESPONSE_BYTES:
                return {"error": "response_too_large"}
            return {"body": output.decode("utf-8")}
    except urllib.error.HTTPError:
        return {"error": "http_error"}
    except TimeoutError:
        return {"error": "timeout"}
    except (urllib.error.URLError, ValueError, UnicodeError, OSError):
        return {"error": "provider_unavailable"}


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = {"error": "provider_unavailable"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))
