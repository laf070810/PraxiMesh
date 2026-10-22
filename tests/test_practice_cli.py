import copy
import io
import json
import os
import stat
import urllib.error
import uuid
from email.message import Message
from pathlib import Path

import pytest

from scripts import practice


@pytest.fixture
def package():
    problem = json.loads(Path("content/starter.json").read_text())["problems"][0]
    public = {key: value for key, value in problem.items() if key not in {"reference", "tests"}}
    return {
        "format": 1,
        "version_id": 1,
        "source_digest": "a" * 64,
        "problem": public,
        "public_tests": [
            {"name": case["name"], "args": case["args"], "expected": case["expected"]}
            for case in problem["tests"]
            if case["public"]
        ],
        "formal_result": False,
    }


@pytest.fixture
def client(package, monkeypatch):
    client = practice.Client("https://example.test")
    monkeypatch.setattr(client, "request", lambda path, **kwargs: copy.deepcopy(package))
    return client


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test",
        "ftp://localhost",
        "https://secret@example.test",
        "https://example.test/a",
        "https://example.test/?x=1",
        "https://example.test/#x",
        "https://example.test:99999",
        "https://example.test\\@attacker.test",
        " https://example.test",
        "http://127.1",
        "http://localhost.attacker.test",
        "https://example.test\n",
    ],
)
def test_origin_rejects_unsafe_or_ambiguous_destinations(value):
    with pytest.raises(practice.PracticeError):
        practice.origin(value, allow_local_http=True)


def test_tls_required_and_loopback_exception_is_explicit():
    assert practice.origin("https://EXAMPLE.TEST/") == "https://example.test"
    with pytest.raises(practice.PracticeError):
        practice.origin("http://127.0.0.1:8000")
    assert practice.origin("http://[::1]:8000", True) == "http://[::1]:8000"


def test_download_creates_private_files_without_overwriting(client, package, tmp_path):
    directory = tmp_path / "exercise"
    result = practice.download(client, package["problem"]["id"], directory)
    assert result["formal_result"] is False
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "solution.py").stat().st_mode) == 0o600
    server, saved, source = practice.load_exercise(directory)
    assert server == client.origin and saved == package and source == package["problem"]["starter"]
    (directory / "solution.py").write_text("my work")
    with pytest.raises(practice.PracticeError):
        practice.download(client, package["problem"]["id"], directory)
    assert (directory / "solution.py").read_text() == "my work"


def test_download_rejects_symlink_destination_and_wrong_problem(
    client, package, tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(practice.PracticeError):
        practice.download(client, package["problem"]["id"], link)
    wrong = copy.deepcopy(package)
    wrong["problem"]["id"] = "different-problem"
    monkeypatch.setattr(client, "request", lambda path: wrong)
    with pytest.raises(practice.PracticeError):
        practice.download(client, package["problem"]["id"], tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_local_files_reject_links_fifos_and_excess_source(client, package, tmp_path):
    directory = tmp_path / "exercise"
    practice.download(client, package["problem"]["id"], directory)
    solution = directory / "solution.py"
    solution.unlink()
    secret = tmp_path / "private"
    secret.write_text("private contents")
    solution.symlink_to(secret)
    with pytest.raises(practice.PracticeError):
        practice.load_exercise(directory)
    solution.unlink()
    os.mkfifo(solution)
    with pytest.raises(practice.PracticeError):
        practice.load_exercise(directory)
    solution.unlink()
    solution.write_text("x" * 20001)
    with pytest.raises(practice.PracticeError):
        practice.load_exercise(directory)


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1,"a":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        '{"x":-1e999}',
        '{"x":"\\ud800"}',
        "[" * 65 + "0" + "]" * 65,
        "[" + ",".join("0" for _ in range(100001)) + "]",
        "[]" * 300000,
    ],
)
def test_bad_json_is_rejected(raw):
    with pytest.raises(practice.PracticeError):
        practice.read_json(raw)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(formal_result=True),
        lambda p: p.update(version_id=True),
        lambda p: p["problem"].update(language="shell"),
        lambda p: p["problem"].update(function="../../anything"),
        lambda p: p["problem"].update(reference="secret"),
        lambda p: p["public_tests"][0].update(public=False),
        lambda p: p["problem"].update(comparison={"kind": "exact", "relative": 1, "absolute": 0}),
    ],
)
def test_untrusted_public_package_cannot_change_execution_contract(package, change):
    change(package)
    with pytest.raises(practice.PracticeError):
        practice.validate_package(package)


def test_complete_program_contract_requires_explicit_stdin_and_json_output(package):
    package["problem"].pop("function")
    package["problem"].update(evaluator="python-stdio-v1", output_format="json")
    package["public_tests"] = [{"name": "example", "stdin": "1\n", "expected": {"value": 1}}]
    assert practice.validate_package(package) == package
    package["public_tests"][0]["args"] = []
    with pytest.raises(practice.PracticeError):
        practice.validate_package(package)


def test_submit_binds_origin_version_source_and_retry_key(client, package, tmp_path, monkeypatch):
    directory = tmp_path / "exercise"
    practice.download(client, package["problem"]["id"], directory)
    captured = []
    monkeypatch.setattr(client, "request", lambda path, **kwargs: captured.append((path, kwargs)))
    key = uuid.uuid4()
    practice.submit(client, directory, "token", key, "submit")
    path, request = captured[0]
    assert path.endswith("/submissions/")
    assert request["payload"] == {
        "version_id": 1,
        "source": package["problem"]["starter"],
        "mode": "submit",
    }
    assert request["key"] == key and request["token"] == "token"
    client.origin = "https://other.test"
    with pytest.raises(practice.PracticeError):
        practice.submit(client, directory, "token", key, "submit")
    assert len(captured) == 1


def test_transport_bearer_headers_bounded_json_and_no_redirect(monkeypatch):
    client = practice.Client("https://example.test")
    raw = "pm_" + "a" * 32 + "." + "b" * 43
    calls = []

    class Response(io.BytesIO):
        status = 202
        headers = Message()
        headers["Content-Type"] = "application/json"

    def opened(request, timeout):
        calls.append(request)
        assert timeout == 30
        return Response(b'{"created":true}')

    monkeypatch.setattr(client.opener, "open", opened)
    key = uuid.uuid4()
    assert client.request(
        "/api/v1/problems/x/submissions/", payload={"source": "private"}, token=raw, key=key
    )["created"]
    assert calls[0].get_header("Authorization") == "Bearer " + raw
    assert calls[0].get_header("Idempotency-key") == str(key)
    assert raw not in calls[0].full_url
    with pytest.raises(practice.PracticeError, match="redirect"):
        practice.NoRedirect().redirect_request(calls[0], None, 302, "", {}, "https://attacker.test")


def test_transport_errors_never_print_token_source_or_response_body(monkeypatch, capsys):
    raw = "pm_" + "a" * 32 + "." + "b" * 43
    client = practice.Client("https://example.test")

    def failed(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.test", 403, raw, {}, io.BytesIO(b"private code")
        )

    monkeypatch.setattr(client.opener, "open", failed)
    with pytest.raises(practice.PracticeError) as caught:
        client.request("/api/v1/submissions/x/", token=raw)
    assert raw not in str(caught.value) and "private code" not in str(caught.value)
    monkeypatch.delenv("PRAXIMESH_API_TOKEN", raising=False)
    assert practice.main(["--server", "https://example.test", "status", str(uuid.uuid4())]) == 2
    assert "PRAXIMESH_API_TOKEN" in capsys.readouterr().err


@pytest.mark.sandbox
def test_real_local_public_tests_are_private_and_not_formal(client, package, tmp_path):
    if os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi":
        pytest.skip("Explicit verified WASI bundle required")
    bundle, digest = os.environ.get("JUDGE_WASI_BUNDLE"), os.environ.get("JUDGE_WASI_SHA256")
    assert bundle and digest
    directory = tmp_path / "exercise"
    practice.download(client, package["problem"]["id"], directory)
    problem = json.loads(Path("content/starter.json").read_text())["problems"][0]
    (directory / "solution.py").write_text(problem["reference"])
    result = practice.local_test(directory, bundle, digest)
    assert result["result"]["verdict"] == "accepted"
    assert result["result"]["total"] == len(package["public_tests"])
    assert result["formal_result"] is False and result["bundle"] == digest
    (directory / "solution.py").write_text("def solve(values): return None")
    assert practice.local_test(directory, bundle, digest)["result"]["verdict"] == "wrong_answer"
