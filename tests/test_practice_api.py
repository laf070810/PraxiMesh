import json
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.hashers import make_password
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import AccountProfile, Problem, Submission, WorkerHealth
from core.queue import configured_fingerprint
from practice_api.models import AccessToken, SubmissionRequest
from practice_api.services import authenticate, export_user_data, issue_token


def url(name, *args):
    return reverse(f"practice_api:{name}", args=args)


def token(user, submit=True, read=True):
    return issue_token(user, "Local tool", 30, can_submit=submit, can_read_results=read)


def headers(raw):
    return {"Authorization": "Bearer " + raw, "Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def ready(settings, db):
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="api-test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )


def payload():
    return {
        "version_id": Problem.objects.get(pk="sum-positive").current_id,
        "source": "def solve(values): return sum(x for x in values if x > 0)",
        "mode": "submit",
    }


def send(client, raw, data=None, request_headers=None):
    return client.post(
        url("submit", "sum-positive"),
        data if data is not None else payload(),
        content_type="application/json",
        headers=request_headers or headers(raw),
    )


def test_public_package_contains_only_reviewed_public_fields(catalog, client):
    response = client.get(url("problem", "sum-positive"))
    data = response.json()
    version = Problem.objects.get(pk="sum-positive").current
    assert data["source_digest"] == version.digest and data["version_id"] == version.pk
    assert data["formal_result"] is False
    assert len(data["public_tests"]) == sum(row["public"] for row in version.data["tests"])
    assert "reference" not in data["problem"] and "tests" not in data["problem"]
    text = json.dumps(data)
    for row in version.data["tests"]:
        if not row["public"]:
            assert row["name"] not in text
    assert client.get(url("problems")).json()["problems"]
    version.problem.published = False
    version.problem.save()
    assert client.get(url("problem", "sum-positive")).status_code == 404


def test_tokens_store_only_digest_and_expire_or_revoke(user):
    item, raw = token(user)
    assert raw not in item.secret_digest and len(item.secret_digest) == 64
    assert authenticate(raw).pk == item.pk
    assert authenticate(raw + "wrong") is None
    assert raw not in json.dumps(export_user_data(user))
    item.expires_at = timezone.now() - timedelta(seconds=1)
    item.save()
    assert authenticate(raw) is None
    item.expires_at = timezone.now() + timedelta(days=1)
    item.revoked_at = timezone.now()
    item.save()
    assert authenticate(raw) is None


def test_inactive_account_token_is_rejected(user):
    _, raw = token(user)
    user.is_active = False
    user.save()
    assert authenticate(raw) is None


@pytest.mark.parametrize(
    "days,submit,read", [(0, True, True), (91, True, True), (True, True, True), (30, False, False)]
)
def test_token_policy_rejects_invalid_expiry_or_empty_scope(user, days, submit, read):
    with pytest.raises(ValueError):
        issue_token(user, "test", days, can_submit=submit, can_read_results=read)


def test_active_token_cap_counts_only_live_tokens(user):
    for _ in range(10):
        token(user)
    with pytest.raises(ValueError):
        token(user)
    AccessToken.objects.filter(user=user).update(revoked_at=timezone.now())
    assert token(user)[0].pk


def test_browser_token_creation_requires_password_and_csrf(catalog, user, logged_client):
    data = {"label": "My computer", "days": 30, "can_submit": True, "can_read_results": True}
    response = logged_client.post(url("tokens"), {**data, "password": "wrong"})
    assert response.status_code == 200 and not AccessToken.objects.exists()
    response = logged_client.post(url("tokens"), {**data, "password": "Test-passphrase-9621"})
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert response.context["raw_token"] and authenticate(response.context["raw_token"])
    assert not logged_client.get(url("tokens")).context["raw_token"]
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post(url("tokens"), data).status_code == 403


def test_revoke_and_listing_are_owner_scoped(catalog, logged_client, user, other_user):
    item, raw = token(other_user)
    assert not list(logged_client.get(url("tokens")).context["page"])
    assert logged_client.post(url("revoke", item.pk)).status_code == 404
    assert authenticate(raw)
    logged_client.force_login(other_user)
    assert logged_client.get(url("revoke", item.pk)).status_code == 405
    assert logged_client.post(url("revoke", item.pk)).status_code == 302
    assert authenticate(raw) is None


def test_api_requires_bearer_even_with_browser_session(catalog, user, logged_client, ready):
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert send(csrf, "").status_code == 401
    _, raw = token(user)
    response = send(csrf, raw)
    assert response.status_code == 202
    job = Submission.objects.get()
    assert job.user == user and job.status == "queued" and not job.verdict
    assert response["Cache-Control"] == "no-store"
    # A bearer cannot authorize an ordinary browser account-management route.
    assert csrf.post(reverse("delete_account"), headers=headers(raw)).status_code == 403


def test_read_only_token_cannot_submit(catalog, user, client, ready):
    _, raw = token(user, submit=False)
    assert send(client, raw).status_code == 403
    assert not Submission.objects.exists()


def test_api_rejects_invalid_unicode_and_throttles_unauthenticated_requests(
    catalog, user, client, ready, monkeypatch
):
    # This assertion concerns one fixed window, not the documented boundary burst.
    import time

    fixed_now = time.time()
    monkeypatch.setattr("core.limits.time.time", lambda: fixed_now)
    _, raw = token(user)
    assert send(client, raw, {**payload(), "source": "\ud800"}).status_code == 400
    assert not Submission.objects.exists()
    for _ in range(119):
        assert send(client, "bad").status_code == 401
    assert send(client, "bad").status_code == 429


def test_real_queue_admission_and_idempotent_replay(catalog, user, client, ready, settings):
    _, raw = token(user)
    request_headers = headers(raw)
    first = send(client, raw, request_headers=request_headers)
    assert first.status_code == 202 and first.json()["created"]
    settings.JUDGE_ENABLED = False
    repeated = send(client, raw, request_headers=request_headers)
    assert repeated.status_code == 202 and not repeated.json()["created"]
    assert repeated.json()["id"] == first.json()["id"]
    assert Submission.objects.count() == SubmissionRequest.objects.count() == 1
    changed = {**payload(), "source": "def solve(values): return -1"}
    assert send(client, raw, changed, request_headers).status_code == 409
    assert send(client, raw).status_code == 503


def test_version_and_queue_quotas_cannot_be_bypassed(catalog, user, client, ready):
    _, raw = token(user)
    assert send(client, raw, {**payload(), "version_id": 999999}).status_code == 409
    assert send(client, raw).status_code == 202
    assert send(client, raw).status_code == 202
    assert send(client, raw).status_code == 429
    assert Submission.objects.count() == 2


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "boolean_version",
        "empty_source",
        "long_source",
        "bad_mode",
        "missing_key",
        "duplicate_key",
    ],
)
def test_api_rejects_forged_or_malformed_input(catalog, user, client, ready, change):
    _, raw = token(user)
    data = payload()
    request_headers = headers(raw)
    if change == "extra":
        data["verdict"] = "accepted"
    elif change == "boolean_version":
        data["version_id"] = True
    elif change == "empty_source":
        data["source"] = " "
    elif change == "long_source":
        data["source"] = "x" * 20001
    elif change == "bad_mode":
        data["mode"] = "accepted"
    elif change == "missing_key":
        request_headers.pop("Idempotency-Key")
    else:
        data = '{"version_id":1,"version_id":2,"source":"source","mode":"submit"}'
    assert send(client, raw, data, request_headers).status_code == 400
    assert not Submission.objects.exists()


def test_result_is_owner_scoped_and_requires_read_scope(catalog, user, other_user, client, ready):
    _, raw = token(user)
    identity = send(client, raw).json()["id"]
    response = client.get(url("result", identity), headers=headers(raw))
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert "source" not in response.json() and not response.json()["formal_result"]
    _, wrong_user = token(other_user)
    assert client.get(url("result", identity), headers=headers(wrong_user)).status_code == 404
    _, write_only = token(user, read=False)
    assert client.get(url("result", identity), headers=headers(write_only)).status_code == 403


def test_recovery_revokes_existing_api_tokens(catalog, user, client):
    _, raw = token(user)
    AccountProfile.objects.create(user=user, recovery_hash=make_password("my-recovery-secret"))
    response = client.post(
        reverse("recover"),
        {
            "username": user.username,
            "recovery_code": "my-recovery-secret",
            "password": "New-secure-passphrase-1832",
        },
    )
    assert response.status_code == 200 and authenticate(raw) is None


def test_export_and_account_deletion_include_api_metadata_without_secrets(
    catalog, user, logged_client, ready
):
    item, raw = token(user)
    assert send(logged_client, raw).status_code == 202
    response = logged_client.post(reverse("export_account"))
    data = response.json()["practice_api"]
    assert data["tokens"][0]["id"] == str(item.pk) and len(data["submission_requests"]) == 1
    assert (
        raw not in response.content.decode() and item.secret_digest not in response.content.decode()
    )
    logged_client.post(
        reverse("delete_account"), {"password": "Test-passphrase-9621", "confirm": "DELETE"}
    )
    assert not AccessToken.objects.exists() and not SubmissionRequest.objects.exists()
