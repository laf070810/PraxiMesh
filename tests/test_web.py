import json
import re

import pytest
from django.contrib.auth.hashers import check_password
from django.test import Client
from django.urls import reverse

from core.models import AccountProfile, Draft, ProblemVersion, Submission


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/learn/",
        "/learn/programming/",
        "/learn/vectors/",
        "/learn/tutorial-functions/",
        "/learn/python-to-numerical/",
        "/practice/",
        "/problems/sum-positive/",
        "/accounts/login/",
        "/accounts/register/",
        "/accounts/recover/",
        "/health/",
    ],
)
def test_public_pages(catalog, client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert "default-src 'self'" in response["Content-Security-Policy"]
    assert response["X-Content-Type-Options"] == "nosniff"


def test_direct_practice_filters(catalog, client):
    response = client.get("/practice/", {"domain": "machine-learning", "q": "点积"})
    text = response.content.decode()
    assert "向量的点积" in text
    assert "只累加正数" not in text
    assert "没有找到匹配" in client.get("/practice/?q=does-not-exist").content.decode()


def test_problem_page_no_protected_data(catalog, client):
    response = client.get("/problems/sum-positive/")
    text = response.content.decode()
    assert "全非正数" not in text and "return sum" not in text
    assert "评测暂不可用" in text


def test_drafts_private_and_safe(catalog, logged_client, user, other_user):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    source = '</textarea><script>alert("xss")</script>'
    response = logged_client.post(
        "/problems/sum-positive/draft/",
        {"source": source, "version": version.pk},
        HTTP_ACCEPT="application/json",
    )
    assert response.json() == {"saved": True}
    page = logged_client.get("/problems/sum-positive/")
    assert b"&lt;/textarea&gt;&lt;script&gt;" in page.content
    assert page["Cache-Control"] == "no-store"
    logged_client.force_login(other_user)
    assert (
        "&lt;/textarea&gt;&lt;script&gt;"
        not in logged_client.get("/problems/sum-positive/").content.decode()
    )
    assert Draft.objects.get(user=user).source == source


def test_disabled_submission_is_503_and_never_queues(catalog, logged_client):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    response = logged_client.post(
        "/problems/sum-positive/submit/",
        {"source": "def solve(v): return 0", "mode": "submit", "version": version.pk},
    )
    assert response.status_code == 503
    assert Submission.objects.count() == 0


def test_submission_requires_login(catalog, client):
    assert client.post("/problems/sum-positive/submit/").status_code == 302


def test_cross_account_submission_and_status_denied(catalog, logged_client, user, other_user):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    job = Submission.objects.create(
        user=user, version=version, source="private-code", source_digest="a" * 64, mode="submit"
    )
    for url in [reverse("submission", args=[job.id]), reverse("submission_status", args=[job.id])]:
        assert logged_client.get(url).status_code == 200
        logged_client.force_login(other_user)
        assert logged_client.get(url).status_code == 404
        logged_client.force_login(user)


def test_csrf_and_post_only(catalog, user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post("/problems/sum-positive/draft/", {"source": "x"}).status_code == 403
    assert client.get("/accounts/logout/").status_code == 405
    assert client.get("/accounts/delete/").status_code == 405
    assert client.get("/problems/sum-positive/submit/").status_code == 405


@pytest.mark.parametrize(
    "recovery_username", ["newlearner", "  newlearner  ", "ｎｅｗｌｅａｒｎｅｒ"]
)
def test_registration_recovery_rotates_token_and_sessions(db, client, recovery_username):
    response = client.post(
        "/accounts/register/",
        {
            "username": "newlearner",
            "password1": "Unique-secret-90328",
            "password2": "Unique-secret-90328",
        },
    )
    assert response.status_code == 200
    token = re.search(r'<pre class="recovery-code">([^<]+)</pre>', response.content.decode()).group(
        1
    )
    profile = AccountProfile.objects.get(user__username="newlearner")
    assert token != profile.recovery_hash and check_password(token, profile.recovery_hash)
    previous_session = client.cookies["sessionid"].value
    second = Client()
    response = second.post(
        "/accounts/recover/",
        {"username": recovery_username, "recovery_code": token, "password": "Another-secret-73104"},
    )
    assert b"recovery-code" in response.content
    profile.refresh_from_db()
    assert not check_password(token, profile.recovery_hash)
    assert profile.user.check_password("Another-secret-73104")
    old = Client()
    old.cookies["sessionid"] = previous_session
    assert old.get("/me/").status_code == 302
    response = Client().post(
        "/accounts/recover/",
        {"username": "newlearner", "recovery_code": token, "password": "Yet-another-secret-8901"},
    )
    assert "用户名或恢复码无效" in response.content.decode()


def test_account_export_and_deletion(catalog, logged_client, user, other_user):
    version = ProblemVersion.objects.first()
    Draft.objects.create(user=user, version=version, source="my-private-source")
    Draft.objects.create(user=other_user, version=version, source="other-private-source")
    response = logged_client.post("/accounts/export/")
    data = json.loads(response.content)
    assert data["drafts"][0]["source"] == "my-private-source"
    assert b"other-private-source" not in response.content
    assert "attachment" in response["Content-Disposition"]
    assert (
        logged_client.post(
            "/accounts/delete/", {"password": "wrong", "confirm": "DELETE"}
        ).status_code
        == 400
    )
    assert (
        logged_client.post(
            "/accounts/delete/", {"password": "Test-passphrase-9621", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    assert not Draft.objects.filter(user_id=user.pk).exists()
    assert Draft.objects.filter(user=other_user).exists()


def test_registration_can_be_closed(db, settings, client):
    settings.REGISTRATION_OPEN = False
    assert client.post("/accounts/register/").status_code == 403


def test_rate_limit_shared_by_account(db, client):
    for _ in range(15):
        client.post("/accounts/login/", {"username": "target", "password": "bad"})
    assert (
        client.post(
            "/accounts/login/", {"username": "target", "password": "bad"}, REMOTE_ADDR="192.0.2.2"
        ).status_code
        == 429
    )


@pytest.mark.parametrize("username", ["  learner  ", "ｌｅａｒｎｅｒ"])
def test_equivalent_login_names_cannot_bypass_account_quota(user, client, username):
    for index in range(15):
        assert (
            client.post(
                "/accounts/login/",
                {"username": "learner", "password": "wrong"},
                REMOTE_ADDR=f"192.0.2.{index + 1}",
            ).status_code
            == 200
        )
    response = client.post(
        "/accounts/login/",
        {"username": username, "password": "Test-passphrase-9621"},
        REMOTE_ADDR="192.0.2.99",
    )
    assert response.status_code == 429
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("username", ["  learner  ", "ｌｅａｒｎｅｒ"])
def test_equivalent_login_names_still_authenticate_with_available_quota(user, client, username):
    response = client.post(
        "/accounts/login/", {"username": username, "password": "Test-passphrase-9621"}
    )
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


def test_stale_editor_cannot_submit_or_overwrite_new_revision(catalog, logged_client):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    response = logged_client.post(
        "/problems/sum-positive/draft/", {"source": "stale", "version": version.pk + 999}
    )
    assert response.status_code == 409
    assert not Draft.objects.exists()
