import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone

from core.middleware import client_address
from core.models import ProblemVersion, RateBucket, Submission, WorkerHealth
from judge.sandbox import DockerSandbox, SandboxUnavailable


def test_proxy_headers_require_explicit_peer_trust(settings):
    factory = RequestFactory()
    request = factory.get("/", REMOTE_ADDR="192.0.2.1", HTTP_X_REAL_IP="198.51.100.1")
    settings.TRUSTED_PROXY_IPS = []
    assert client_address(request) == "192.0.2.1"
    settings.TRUSTED_PROXY_IPS = ["192.0.2.1"]
    assert client_address(request) == "198.51.100.1"
    request.META["HTTP_X_REAL_IP"] = "198.51.100.1, 192.0.2.2"
    assert client_address(request) == "192.0.2.1"


@pytest.mark.parametrize(
    "peer,protocol,expected",
    [
        ("192.0.2.5", "https", 301),
        ("192.0.2.5", "https,http", 301),
        ("10.0.0.2", "https", 200),
        ("10.0.0.2", "http", 301),
        ("10.0.0.2", "https,http", 301),
    ],
)
def test_tls_forwarding_requires_trusted_peer_and_one_protocol(
    db, client, settings, peer, protocol, expected
):
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    settings.TRUSTED_PROXY_IPS = ["10.0.0.2"]
    response = client.get("/health/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_PROTO=protocol)
    assert response.status_code == expected


def test_maintenance_works_without_worker(catalog, user):
    now = timezone.now()
    job = Submission.objects.create(
        user=user,
        version=ProblemVersion.objects.first(),
        source="private",
        source_digest="a" * 64,
        mode="submit",
    )
    Submission.objects.filter(pk=job.pk).update(created_at=now - timedelta(minutes=11))
    WorkerHealth.objects.create(
        id="dead", checked_at=now - timedelta(minutes=2), ready=True, fingerprint="old"
    )
    RateBucket.objects.create(key="expired", count=1, expires_at=now - timedelta(seconds=1))
    call_command("maintain")
    job.refresh_from_db()
    assert job.status == "finished" and job.verdict == "internal_error"
    assert not WorkerHealth.objects.get(pk="dead").ready
    assert not RateBucket.objects.exists()


def test_container_exit_during_limit_cleanup_is_not_platform_failure(monkeypatch):
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)
    calls = iter(
        [
            b'{"Running":true}',
            SandboxUnavailable("already stopped"),
            b'{"Running":false,"Error":""}',
        ]
    )

    def command(*args, **kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(sandbox, "command", command)
    sandbox._kill_if_running("example")


def test_container_cleanup_does_not_hide_a_still_running_guest(monkeypatch):
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)

    def command(args):
        if args[0] == "kill":
            raise SandboxUnavailable("failed to kill")
        return b'{"Running":true}'

    monkeypatch.setattr(sandbox, "command", command)
    with pytest.raises(SandboxUnavailable):
        sandbox._kill_if_running("example")


def test_preflight_accepts_normalized_docker_repo_digest(monkeypatch):
    digest = "sha256:" + "a" * 64
    sandbox = DockerSandbox("python:3.11.15-slim-bookworm@" + digest)
    info = {
        "Runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}},
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
        "CPUCfsQuota": True,
    }
    image_info = [
        {
            "RepoDigests": ["docker.io/library/python@" + digest],
            "Config": {},
            "Id": "image-id",
            "Architecture": "amd64",
        }
    ]
    responses = iter([json.dumps(info).encode(), json.dumps(image_info).encode()])
    monkeypatch.setattr(sandbox, "command", lambda *a, **kw: next(responses))
    assert sandbox.preflight()["image"] == sandbox.image


def test_deleted_job_does_not_crash_worker(catalog, user, settings, monkeypatch):
    from core.management.commands import judge_worker
    from core.queue import configured_fingerprint

    settings.JUDGE_ENABLED = True
    job = Submission.objects.create(
        user=user,
        version=ProblemVersion.objects.first(),
        source="source",
        source_digest="a" * 64,
        evaluation_fingerprint=configured_fingerprint(),
        mode="submit",
    )
    sandbox = Mock()
    sandbox.preflight.return_value = {}
    monkeypatch.setattr(judge_worker, "configured_sandbox", lambda: sandbox)
    monkeypatch.setattr(judge_worker, "check_receipt", lambda identity: None)

    def cancel(version, source, mode, backend, on_case):
        job.delete()
        on_case()
        raise AssertionError("Deleted task must not continue execution")

    monkeypatch.setattr(judge_worker, "evaluate", cancel)
    call_command("judge_worker", once=True, worker_id="test-deletion")
    assert not Submission.objects.exists()
    sandbox.execute.assert_not_called()
