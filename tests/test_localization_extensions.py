"""Interface translation checks; formal-result fixtures are not sandbox acceptance."""

import pytest
from django.utils import translation

from core.models import Problem
from localization.packages import import_translations, load_translations
from practice_api.models import AccessToken
from showcase.models import PublicProfile


@pytest.fixture
def english_extensions(catalog, settings, client):
    import_translations(load_translations(settings.CONTENT_ROOT / "en.json"))
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = "en-US"
    yield client
    translation.deactivate()


@pytest.mark.parametrize(
    "path,heading",
    [
        ("/accounts/tokens/", "Local tool access tokens"),
        ("/showcase/settings/", "Public profile settings"),
        ("/showcase/mine/", "Manage my shares"),
        ("/showcase/boards/", "Grouped test-progress leaderboards"),
    ],
)
def test_new_english_interfaces(english_extensions, user, path, heading):
    client = english_extensions
    client.force_login(user)
    response = client.get(path)
    assert response.status_code == 200 and heading in response.content.decode()
    assert response["Content-Language"] == "en"


def test_english_token_form_create_and_revoke(english_extensions, user):
    client = english_extensions
    client.force_login(user)
    response = client.post(
        "/accounts/tokens/",
        {"label": "用户原始用途", "days": 30, "can_submit": "on", "password": "wrong"},
    )
    assert "Incorrect password." in response.content.decode()
    assert not AccessToken.objects.exists()
    response = client.post(
        "/accounts/tokens/",
        {
            "label": "用户原始用途",
            "days": 30,
            "can_submit": "on",
            "password": "Test-passphrase-9621",
        },
    )
    assert "Save this token now" in response.content.decode()
    token = AccessToken.objects.get(user=user)
    assert token.label == "用户原始用途"
    response = client.post(f"/accounts/tokens/{token.pk}/revoke/", follow=True)
    assert response.status_code == 200 and "Revoked" in response.content.decode()
    token.refresh_from_db()
    assert token.revoked_at is not None


def test_machine_problem_package_is_not_rewritten_by_interface_language(english_extensions):
    response = english_extensions.get("/api/v1/problems/sum-positive/")
    assert response.status_code == 200
    source = Problem.objects.get(pk="sum-positive").current
    assert response.json()["problem"]["title"] == source.data["title"]
    assert response.json()["source_digest"] == source.digest
    assert "reference" not in response.json()["problem"]


def test_english_public_profile_retains_user_text_and_exact_version_label(english_extensions, user):
    from test_showcase import job

    client = english_extensions
    client.force_login(user)
    response = client.post(
        "/showcase/settings/",
        {
            "revision": 0,
            "public_name": "我的公开名",
            "bio": "公开原文 <script>unsafe</script>",
            "is_public": "on",
            "confirm_public": "on",
        },
    )
    assert response.status_code == 302
    profile = PublicProfile.objects.get(user=user)
    item = job(user)
    preview = client.get(f"/showcase/share/{item.pk}/")
    assert "Sum only positive values" in preview.content.decode()
    assert "Accepted" in preview.content.decode()
    assert "PRIVATE-SOURCE-SENTINEL" not in preview.content.decode()
    response = client.post(
        f"/showcase/share/{item.pk}/",
        {"profile_revision": profile.revision, "entry_revision": 0, "confirm_public": "on"},
        follow=True,
    )
    assert response.status_code == 200 and "Sum only positive values" in response.content.decode()
    assert "Accepted" in response.content.decode()
    page = client.get(f"/showcase/profiles/{user.username}/")
    assert (
        "我的公开名" in page.content.decode()
        and "&lt;script&gt;unsafe&lt;/script&gt;" in page.content.decode()
    )
    assert "PRIVATE-SOURCE-SENTINEL" not in page.content.decode()
    board = client.get(f"/showcase/boards/{item.version_id}/{item.evaluation_fingerprint}/")
    assert board.status_code == 200 and "Sum only positive values" in board.content.decode()
    assert "Sequential tests passed" in board.content.decode()
    Problem.objects.filter(pk=item.version.problem_id).update(published=False)
    assert client.get(response.redirect_chain[-1][0]).status_code == 404
    assert (
        client.get(f"/showcase/boards/{item.version_id}/{item.evaluation_fingerprint}/").status_code
        == 404
    )
