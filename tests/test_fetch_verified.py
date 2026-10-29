import hashlib
import io
import urllib.error

import pytest

from scripts import fetch_verified


def test_transient_download_retries_without_publishing_partial_bytes(tmp_path, monkeypatch):
    content = b"verified build input"
    calls = []

    def open_url(url, timeout):
        calls.append(url)
        assert not (tmp_path / "archive").exists()
        if len(calls) == 1:
            raise urllib.error.URLError("temporary failure")
        return io.BytesIO(content)

    monkeypatch.setattr(fetch_verified.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(fetch_verified.time, "sleep", lambda _: None)
    target = tmp_path / "archive"
    fetch_verified.download(
        "https://example.invalid/input", target, hashlib.sha256(content).hexdigest()
    )
    assert target.read_bytes() == content and len(calls) == 2
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("reason", ["digest", "size"])
def test_untrusted_bytes_are_not_published_or_retried(tmp_path, monkeypatch, reason):
    calls = []

    def open_url(url, timeout):
        calls.append(url)
        return io.BytesIO(b"unexpected bytes")

    monkeypatch.setattr(fetch_verified.urllib.request, "urlopen", open_url)
    with pytest.raises(ValueError):
        fetch_verified.download(
            "https://example.invalid/input",
            tmp_path / "archive",
            "a" * 64,
            maximum=1 if reason == "size" else 100,
        )
    assert len(calls) == 1 and list(tmp_path.iterdir()) == []


def test_retries_are_bounded_and_leave_no_input(tmp_path, monkeypatch):
    calls = []

    def open_url(url, timeout):
        calls.append(url)
        raise TimeoutError

    monkeypatch.setattr(fetch_verified.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(fetch_verified.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError):
        fetch_verified.download("https://example.invalid/input", tmp_path / "archive", "a" * 64)
    assert len(calls) == 3 and list(tmp_path.iterdir()) == []


def test_existing_input_is_never_overwritten(tmp_path):
    target = tmp_path / "archive"
    target.write_bytes(b"existing")
    with pytest.raises(ValueError):
        fetch_verified.download("https://example.invalid/input", target, "a" * 64)
    assert target.read_bytes() == b"existing"
