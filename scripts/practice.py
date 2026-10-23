"""Download public exercises, test in WASI, and submit to a trusted server.

Network commands use only the Python standard library. Local tests additionally
require this checkout's locked judge dependencies and a verified WASI bundle.
No downloaded or submitted program is executed by the host interpreter.
"""

import argparse
import json
import math
import os
import re
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_RESPONSE = 512000
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
TOKEN = re.compile(r"pm_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")
LANGUAGES = {
    "python": "solution.py",
    "javascript": "solution.js",
}
CONTRACTS = {
    "python-function-v1": "python",
    "python-object-api-v1": "python",
    "javascript-function-v1": "javascript",
    "python-stdio-v1": "python",
    "python-ml-linear-v1": "python",
    "python-weighted-fit-v1": "python",
    "python-agent-tools-v1": "python",
}
ROOT = Path(__file__).resolve().parent.parent
SPECIALISTS = {"python-agent-tools-v1", "python-ml-linear-v1", "python-weighted-fit-v1"}


def specialist_module(evaluator):
    sys.path.insert(0, str(ROOT))
    from judge import agent, ml, scientific

    return {
        "python-agent-tools-v1": agent,
        "python-ml-linear-v1": ml,
        "python-weighted-fit-v1": scientific,
    }[evaluator]


def specialist_payload(package):
    problem = package["problem"]
    module = specialist_module(problem["evaluator"])
    keys = (
        module.PUBLIC_KEYS
        if problem["evaluator"] == "python-agent-tools-v1"
        else module.PUBLIC_FIELDS
    )
    payload = {key: problem[key] for key in keys - {"tests"}}
    payload["tests"] = [{**case, "public": True} for case in package["public_tests"]]
    # Do not silently replace an untrusted visibility field with True.
    if any("public" in case for case in package["public_tests"]):
        raise ValueError("Public cases must not contain a visibility override")
    module.validate_public_problem(payload)
    return payload


class PracticeError(ValueError):
    """A safe error without credentials, source code or server response bodies."""


def api_payload(package):
    sys.path.insert(0, str(ROOT))
    from judge.object_api import validate_problem

    problem = package["problem"]
    if any(
        key in problem
        for key in ("function", "output_format", "assessment", "model_kind", "uncertainty_model")
    ):
        raise ValueError("Mixed object API contract")
    payload = {key: problem[key] for key in ("evaluator", "language", "interface", "comparison")}
    if any("public" in case for case in package["public_tests"]):
        raise ValueError("Public cases must not contain a visibility override")
    payload["tests"] = [{**case, "public": True} for case in package["public_tests"]]
    validate_problem(payload, public_only=True)
    return payload


def validate_json_tree(value):
    """Bound untrusted documents before using them as a local exercise."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("JSON structure limit")
        if isinstance(item, str):
            item.encode("utf-8")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Nonfinite number")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Non-string key")
                pending.extend(((key, depth + 1), (child, depth + 1)))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (bool, int):
            raise ValueError("Unsupported JSON value")


def read_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Nonfinite number")

    try:
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_RESPONSE:
            raise ValueError("Too large")
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        validate_json_tree(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PracticeError("Invalid or oversized JSON document.") from None


def origin(server, allow_local_http=False):
    try:
        parts = urllib.parse.urlsplit(server)
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or any(ord(character) <= 32 for character in server)
            or "\\" in server
        ):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        local_http = (
            allow_local_http
            and parts.scheme == "http"
            and parts.hostname in ("127.0.0.1", "::1", "localhost")
        )
        if parts.scheme != "https" and not local_http:
            raise ValueError
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), "", "", ""))
    except ValueError:
        raise PracticeError(
            "Choose an HTTPS server origin without credentials or a path. "
            "--allow-local-http permits only an explicit loopback development server."
        ) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PracticeError("Server redirect refused. Check the configured server origin.")


class Client:
    def __init__(self, server, *, allow_local_http=False, ca_file=None):
        self.origin = origin(server, allow_local_http)
        try:
            context = ssl.create_default_context(cafile=ca_file)
        except (OSError, ssl.SSLError):
            raise PracticeError("Cannot load the configured certificate authority.") from None
        # A workstation proxy must not silently become another credential recipient.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(self, path, *, payload=None, token=None, key=None):
        if not path.startswith("/api/v1/") or any(c in path for c in "?#\\\r\n"):
            raise PracticeError("Invalid API path.")
        headers = {"Accept": "application/json", "User-Agent": "PraxiMesh-Practice/1"}
        if token is not None:
            if not TOKEN.fullmatch(token):
                raise PracticeError("PRAXIMESH_API_TOKEN is missing or has an invalid format.")
            headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = str(uuid.UUID(str(key)))
        request = urllib.request.Request(self.origin + path, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 202):
                    raise PracticeError("Unexpected API response status.")
                if response.headers.get_content_type() != "application/json":
                    raise PracticeError("Server did not return JSON.")
                return read_json(response.read(MAX_RESPONSE + 1))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            message = {
                401: "Token is invalid, expired or revoked.",
                403: "Token lacks the required permission.",
                404: "The requested problem or your submission is unavailable.",
                409: "Version or idempotency conflict. Check the downloaded version and request key.",
                429: "Rate or pending-submission limit reached. Retry later.",
                503: "Evaluation is currently unavailable. Your local files are preserved.",
            }.get(status, f"Server rejected the request (HTTP {status}).")
            raise PracticeError(message) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise PracticeError(
                "Cannot reach the server securely. A submission may have arrived; "
                "retry with the same request key."
            ) from None


def validate_package(package):
    try:
        validate_json_tree(package)
        if not isinstance(package, dict) or set(package) != {
            "format",
            "version_id",
            "source_digest",
            "problem",
            "public_tests",
            "formal_result",
        }:
            raise ValueError
        problem = package["problem"]
        if (
            type(package["format"]) is not int
            or package["format"] != 1
            or type(package["version_id"]) is not int
            or package["version_id"] < 1
            or package["formal_result"] is not False
            or not re.fullmatch(r"[a-f0-9]{64}", package["source_digest"])
            or not isinstance(problem, dict)
            or not SLUG.fullmatch(problem["id"])
            or problem["language"] not in LANGUAGES
            or CONTRACTS.get(problem["evaluator"]) != problem["language"]
            or not isinstance(problem["starter"], str)
            or len(problem["starter"]) > 20000
            or "reference" in problem
            or "tests" in problem
        ):
            raise ValueError
        stdio = problem["evaluator"] == "python-stdio-v1"
        if problem["evaluator"] != "python-object-api-v1" and "interface" in problem:
            raise ValueError
        if stdio:
            if "function" in problem or problem.get("output_format") != "json":
                raise ValueError
        elif problem["evaluator"] == "python-object-api-v1":
            api_payload(package)
        elif "output_format" in problem or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,79}", problem["function"]
        ):
            raise ValueError
        comparison = problem["comparison"]
        if comparison["kind"] == "exact":
            if set(comparison) != {"kind", "relative", "absolute"} or any(
                type(comparison[key]) not in (int, float) or comparison[key] != 0
                for key in ("relative", "absolute")
            ):
                raise ValueError
        elif comparison["kind"] == "numeric":
            if set(comparison) != {"kind", "relative", "absolute"}:
                raise ValueError
            for key in ("relative", "absolute"):
                value = comparison[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError
        else:
            raise ValueError
        cases = package["public_tests"]
        if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
            raise ValueError
        if problem["evaluator"] in SPECIALISTS:
            specialist_payload(package)
            return package
        if problem["evaluator"] == "python-object-api-v1":
            return package
        for case in cases:
            if (
                not isinstance(case, dict)
                or set(case) != {"name", "stdin" if stdio else "args", "expected"}
                or not isinstance(case["name"], str)
                or len(case["name"]) > 200
                or not isinstance(case["stdin"] if stdio else case["args"], str if stdio else list)
            ):
                raise ValueError
        return package
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise PracticeError("Unsupported or invalid public exercise package.") from None


def download(client, slug, directory):
    if not SLUG.fullmatch(slug):
        raise PracticeError("Invalid problem ID.")
    package = validate_package(client.request(f"/api/v1/problems/{slug}/"))
    if package["problem"]["id"] != slug:
        raise PracticeError("Server returned a different problem.")
    directory = Path(directory)
    try:
        directory.mkdir(mode=0o700)
    except OSError:
        raise PracticeError(
            "Destination must be a new directory with an existing parent."
        ) from None
    filename = LANGUAGES[package["problem"]["language"]]
    files = {
        "exercise.json": json.dumps(
            {"server": client.origin, "package": package}, ensure_ascii=False, indent=2
        ),
        filename: package["problem"]["starter"],
    }
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, content in files.items():
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
        finally:
            os.close(descriptor)
    except OSError:
        raise PracticeError(
            "Could not write the new exercise directory; inspect it before retrying."
        ) from None
    return {
        "problem": slug,
        "version_id": package["version_id"],
        "solution": filename,
        "formal_result": False,
    }


def read_file(path, maximum):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise ValueError
            return content.decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        raise PracticeError(
            "Exercise files must be bounded, regular UTF-8 files without symlinks."
        ) from None


def load_exercise(directory):
    directory = Path(directory)
    document = read_json(read_file(directory / "exercise.json", MAX_RESPONSE))
    if not isinstance(document, dict) or set(document) != {"server", "package"}:
        raise PracticeError("Invalid exercise metadata.")
    package = validate_package(document["package"])
    filename = LANGUAGES[package["problem"]["language"]]
    source = read_file(directory / filename, 80000)
    if not source.strip() or len(source) > 20000:
        raise PracticeError("Solution must contain between 1 and 20,000 characters.")
    return document["server"], package, source


def local_test(directory, bundle, digest):
    _, package, source = load_exercise(directory)
    sys.path.insert(0, str(ROOT))
    from judge.evaluator import EVALUATORS, evaluate
    from judge.sandbox import SandboxUnavailable
    from judge.wasi import WasiSandbox

    problem = package["problem"]
    evaluator = problem["evaluator"]
    if evaluator not in EVALUATORS:
        raise PracticeError("This checkout does not support this exercise language.")
    try:
        sandbox = WasiSandbox(bundle, digest)
        environment = sandbox.preflight()
        if evaluator in SPECIALISTS:
            result = specialist_module(evaluator).evaluate_public(
                specialist_payload(package), source, sandbox
            )
        elif evaluator == "python-object-api-v1":
            from judge.object_api import evaluate_public

            result = evaluate_public(api_payload(package), source, sandbox)
        else:
            result = evaluate(
                {
                    "evaluator": evaluator,
                    **(
                        {"output_format": problem["output_format"]}
                        if evaluator == "python-stdio-v1"
                        else {"function": problem["function"]}
                    ),
                    "comparison": problem["comparison"],
                    "tests": [{**case, "public": True} for case in package["public_tests"]],
                },
                source,
                "run",
                sandbox,
            )
    except SandboxUnavailable:
        raise PracticeError(
            "Verified local WASI execution is unavailable. Check bundle and dependencies."
        ) from None
    return {
        "scope": "Local public tests only; this does not change server progress or ranking.",
        "formal_result": False,
        "version_id": package["version_id"],
        "bundle": environment["bundle"],
        "policy_digest": environment["policy_digest"],
        "result": result,
    }


def submit(client, directory, token, key, mode):
    server, package, source = load_exercise(directory)
    if server != client.origin:
        raise PracticeError(
            "Exercise origin differs from --server. Download from the intended server."
        )
    slug = package["problem"]["id"]
    return client.request(
        f"/api/v1/problems/{slug}/submissions/",
        payload={"version_id": package["version_id"], "source": source, "mode": mode},
        token=token,
        key=key,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="Explicit HTTPS origin for network commands")
    parser.add_argument("--ca-file", help="Additional trusted CA certificate file")
    parser.add_argument("--allow-local-http", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("download", help="Download public examples into a new directory")
    get.add_argument("problem")
    get.add_argument("directory", type=Path)
    run = commands.add_parser(
        "test", help="Run public examples inside the verified local WASI sandbox"
    )
    run.add_argument("directory", type=Path)
    run.add_argument("--bundle", required=True)
    run.add_argument("--bundle-sha256", required=True)
    send = commands.add_parser(
        "submit", help="Submit source to the server's trusted evaluation queue"
    )
    send.add_argument("directory", type=Path)
    send.add_argument("--mode", choices=("run", "submit"), default="submit")
    send.add_argument(
        "--request-key", type=uuid.UUID, help="Reuse the printed UUID after a connection failure"
    )
    status = commands.add_parser("status", help="Read your own server submission")
    status.add_argument("submission", type=uuid.UUID)
    args = parser.parse_args(argv)
    try:
        if args.command == "test":
            result = local_test(args.directory, args.bundle, args.bundle_sha256)
        else:
            if not args.server:
                raise PracticeError("Network commands require an explicit --server origin.")
            client = Client(
                args.server, allow_local_http=args.allow_local_http, ca_file=args.ca_file
            )
            if args.command == "download":
                result = download(client, args.problem, args.directory)
            else:
                token = os.environ.get("PRAXIMESH_API_TOKEN", "")
                if not TOKEN.fullmatch(token):
                    raise PracticeError(
                        "Set PRAXIMESH_API_TOKEN using your private shell environment."
                    )
                if args.command == "submit":
                    key = args.request_key or uuid.uuid4()
                    print(f"Request key: {key}", file=sys.stderr, flush=True)
                    result = submit(client, args.directory, token, key, args.mode)
                else:
                    result = client.request(f"/api/v1/submissions/{args.submission}/", token=token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.command != "test" or result["result"]["verdict"] == "accepted" else 1
    except (PracticeError, ImportError) as exc:
        message = (
            str(exc)
            if isinstance(exc, PracticeError)
            else "Install this checkout's locked judge dependencies."
        )
        print(f"Practice: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
