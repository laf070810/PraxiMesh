"""Blog HTTP workflows reuse community governance and public visibility boundaries."""

import json

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from community.models import Appeal, Comment, ModerationEvent, Post, Report, State
from core.models import AccountProfile, Problem, Submission
from showcase.models import PublicProfile

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse("community:" + name, args=args)


@pytest.fixture
def reviewer(django_user_model):
    account = django_user_model.objects.create_user(
        username="blog-reviewer", password="Moderate-Password-2048"
    )
    account.user_permissions.add(
        Permission.objects.get(content_type__app_label="community", codename="moderate_content")
    )
    assert not account.is_staff and not account.is_superuser
    return account


def create_blog(client, author, **extra):
    client.force_login(author)
    data = {"kind": "blog", "title": "Original blog title", "body": "Original blog body", **extra}
    response = client.post(url("create"), data)
    assert response.status_code == 302
    post = Post.objects.get(author=author, title=data["title"])
    assert post.state == State.DRAFT
    return post


def author_action(client, post, action):
    client.force_login(post.author)
    response = client.post(
        url("author_action", "post", post.pk, action), {"revision": post.revision}
    )
    assert response.status_code == 302
    if action != "delete":
        post.refresh_from_db()


def moderate(client, reviewer, post, action):
    client.force_login(reviewer)
    response = client.post(
        url("moderate", "post", post.pk),
        {"revision": post.revision, "action": action, "note": "Reviewed current blog revision"},
    )
    assert response.status_code == 302
    post.refresh_from_db()


def publish(client, reviewer, post):
    author_action(client, post, "submit")
    assert post.state == State.PENDING
    moderate(client, reviewer, post, "publish")
    assert post.state == State.PUBLISHED


def test_blog_preselection_private_default_and_no_private_source_import(
    catalog, client, user, other_user, reviewer
):
    version = Problem.objects.get(pk="sum-positive").current
    job = Submission.objects.create(
        user=user, version=version, source="DO-NOT-COPY-PRIVATE-SOURCE", source_digest="a" * 64
    )
    AccountProfile.objects.create(user=user, display_name="PRIVATE-DISPLAY-NAME", recovery_hash="x")
    client.force_login(user)
    assert client.get(url("create"), {"kind": "blog"}).context["form"].initial["kind"] == "blog"
    post = create_blog(client, user, submission=job.pk, state="published")
    assert post.body == "Original blog body" and post.version_id is None
    assert not PublicProfile.objects.filter(user=user).exists()
    guest = Client()
    for account in (user, other_user, reviewer):
        client.force_login(account)
        assert client.get(url("author_blog", user.username)).status_code == 404
        if account != user:
            assert client.get(url("detail", post.pk)).status_code == 404
    assert guest.get(url("author_blog", "nonexistent-user")).status_code == 404
    assert post.title not in guest.get(url("blogs")).content.decode()
    publish(client, reviewer, post)
    for endpoint in (url("detail", post.pk), url("blogs"), url("author_blog", user.username)):
        body = guest.get(endpoint).content.decode()
        assert post.title in body
        assert "DO-NOT-COPY-PRIVATE-SOURCE" not in body and "PRIVATE-DISPLAY-NAME" not in body
    assert b"Blog post" in guest.get(url("detail", post.pk), HTTP_ACCEPT_LANGUAGE="en").content


def test_blog_edit_hide_appeal_review_and_withdraw_share_locked_lifecycle(
    client, user, reviewer, other_user
):
    post = create_blog(client, user)
    guest = Client()
    publish(client, reviewer, post)
    client.force_login(user)
    response = client.post(
        url("edit", post.pk),
        {"kind": "blog", "title": "Edited blog", "body": "Edited private text", "revision": 1},
    )
    assert response.status_code == 302
    post.refresh_from_db()
    assert post.revision == 2 and post.state == State.PENDING
    assert guest.get(url("detail", post.pk)).status_code == 404
    assert guest.get(url("author_blog", user.username)).status_code == 404
    client.force_login(reviewer)
    assert (
        client.post(
            url("moderate", "post", post.pk),
            {"revision": 1, "action": "publish", "note": "Stale review"},
        ).status_code
        == 409
    )
    moderate(client, reviewer, post, "publish")
    moderate(client, reviewer, post, "hide")
    assert post.state == State.HIDDEN and guest.get(url("detail", post.pk)).status_code == 404
    client.force_login(user)
    assert (
        client.post(
            url("appeal_create", "post", post.pk),
            {"reason": "AUTHOR-PRIVATE-APPEAL", "revision": post.revision},
        ).status_code
        == 302
    )
    appeal = Appeal.objects.get(post=post)
    client.force_login(other_user)
    assert client.get(url("appeal_detail", appeal.pk)).status_code == 404
    client.force_login(reviewer)
    assert (
        client.post(
            url("resolve_case", "appeal", appeal.pk),
            {"decision": "reopened", "note": "Reopened for an independent review"},
        ).status_code
        == 302
    )
    post.refresh_from_db()
    assert post.state == State.PENDING
    assert guest.get(url("author_blog", user.username)).status_code == 404
    moderate(client, reviewer, post, "publish")
    author_action(client, post, "withdraw")
    assert post.state == State.WITHDRAWN
    assert guest.get(url("author_blog", user.username)).status_code == 404
    assert guest.get(url("detail", post.pk)).status_code == 404
    author_action(client, post, "delete")
    assert not Post.objects.filter(pk=post.pk).exists() and not Appeal.objects.exists()
    assert ModerationEvent.objects.exists()


def test_blog_comment_and_report_keep_parent_visibility_and_case_privacy(
    client, user, reviewer, other_user
):
    post = create_blog(client, user)
    publish(client, reviewer, post)
    client.force_login(other_user)
    assert (
        client.post(url("comment_create", post.pk), {"body": "Public comment"}).status_code == 302
    )
    comment = Comment.objects.get(post=post)
    assert (
        client.post(
            url("author_action", "comment", comment.pk, "submit"), {"revision": 1}
        ).status_code
        == 302
    )
    client.force_login(reviewer)
    assert (
        client.post(
            url("moderate", "comment", comment.pk),
            {"revision": 1, "action": "publish", "note": "Reviewed comment"},
        ).status_code
        == 302
    )
    client.force_login(other_user)
    assert (
        client.post(
            url("report_create", "post", post.pk), {"reason": "REPORTER-PRIVATE-REASON"}
        ).status_code
        == 302
    )
    report = Report.objects.get(post=post)
    client.force_login(user)
    assert client.get(url("report_detail", report.pk)).status_code == 404
    client.force_login(reviewer)
    assert (
        client.post(
            url("resolve_case", "report", report.pk),
            {"decision": "resolved", "note": "REPORTER-PRIVATE-RESOLUTION"},
        ).status_code
        == 302
    )
    assert Client().get(url("comment_detail", comment.pk)).status_code == 200
    moderate(client, reviewer, post, "hide")
    guest = Client()
    assert guest.get(url("comment_detail", comment.pk)).status_code == 404
    assert guest.get(url("author_blog", user.username)).status_code == 404
    client.force_login(other_user)
    assert client.get(url("comment_detail", comment.pk)).status_code == 404
    assert client.get(url("report_create", "comment", comment.pk)).status_code == 404
    assert client.get(url("report_detail", report.pk)).status_code == 200


def test_blog_directory_paginates_only_public_blogs_for_the_exact_author(client, user, other_user):
    # Listing fixtures are metadata, not fabricated judging or review evidence.
    Post.objects.bulk_create(
        [
            Post(author=user, kind="blog", state="published", title=f"Public {i}", body="text")
            for i in range(21)
        ]
        + [
            Post(
                author=other_user, kind="blog", state="published", title="Other author", body="text"
            )
        ]
        + [Post(author=user, kind="discussion", state="published", title="Discussion", body="text")]
        + [
            Post(author=user, kind="blog", state=state, title=f"Private-{state}", body="text")
            for state in State.values
            if state != "published"
        ]
    )
    response = client.get(url("author_blog", user.username))
    assert len(response.context["page"]) == 20
    body = response.content.decode()
    assert "?kind=&amp;problem=&amp;page=2" in body
    assert "Other author" not in body and "Discussion" not in body and "Private-" not in body
    assert len(client.get(url("author_blog", user.username), {"page": 2}).context["page"]) == 1
    assert client.get(url("index"), {"kind": "blog"}).context["page"].paginator.count == 22
    assert client.post(url("blogs"), {}).status_code == 405
    assert client.post(url("author_blog", user.username), {}).status_code == 405


def test_linked_problem_withdrawal_removes_blog_from_all_public_directories(
    catalog, client, user, reviewer
):
    version = Problem.objects.get(pk="sum-positive").current
    post = create_blog(client, user, version=version.pk)
    publish(client, reviewer, post)
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    guest = Client()
    assert guest.get(url("author_blog", user.username)).status_code == 404
    assert guest.get(url("detail", post.pk)).status_code == 404
    assert post.title not in guest.get(url("blogs")).content.decode()
    assert post.title not in guest.get(url("index"), {"kind": "blog"}).content.decode()
    client.force_login(user)
    assert client.get(url("detail", post.pk)).status_code == 200


def test_blog_export_and_account_deletion_cover_original_text_and_private_cases(
    client, user, reviewer, other_user
):
    post = create_blog(client, user)
    publish(client, reviewer, post)
    client.force_login(other_user)
    assert client.post(url("comment_create", post.pk), {"body": "Other comment"}).status_code == 302
    assert (
        client.post(
            url("report_create", "post", post.pk), {"reason": "OTHER-REPORT-SECRET"}
        ).status_code
        == 302
    )
    client.force_login(user)
    export = client.post(reverse("export_account")).json()
    assert export["community"]["posts"][0]["kind"] == "blog"
    assert export["community"]["posts"][0]["body"] == post.body
    assert "OTHER-REPORT-SECRET" not in json.dumps(export)
    response = client.post(
        reverse("delete_account"), {"password": "Test-passphrase-9621", "confirm": "DELETE"}
    )
    assert response.status_code == 200
    assert (
        not Post.objects.exists() and not Comment.objects.exists() and not Report.objects.exists()
    )
    assert Client().get(url("author_blog", user.username)).status_code == 404
    assert ModerationEvent.objects.exists()
    client.force_login(reviewer)
    assert (
        client.post(
            reverse("delete_account"), {"password": "Moderate-Password-2048", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    assert not ModerationEvent.objects.exclude(actor=None).exists()


@pytest.mark.parametrize(
    "locale,heading", [("en-US", "Blogs and learning experiences"), ("zh-CN", "博客与学习经验")]
)
def test_blog_interface_is_localized_and_original_text_is_escaped(
    client, user, reviewer, locale, heading
):
    post = create_blog(
        client, user, title="中文作者原文 <script>unsafe()</script>", body="中文正文 <img src=x>"
    )
    publish(client, reviewer, post)
    guest = Client(HTTP_ACCEPT_LANGUAGE=locale)
    body = guest.get(url("blogs")).content.decode()
    assert heading in body and "&lt;script&gt;" in body and "<script>unsafe" not in body
    article = guest.get(url("detail", post.pk)).content.decode()
    assert "中文正文 &lt;img src=x&gt;" in article


def test_blog_cannot_bypass_auth_csrf_rate_limits_or_moderator_permission(
    client, user, other_user, monkeypatch
):
    assert client.post(url("create"), {"kind": "blog"}).status_code == 302
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post(url("create"), {"kind": "blog", "title": "x", "body": "x"}).status_code == 403
    post = create_blog(client, user)
    author_action(client, post, "submit")
    other_user.is_staff = True
    other_user.save(update_fields=["is_staff"])
    client.force_login(other_user)
    assert client.get(url("moderation")).status_code == 403
    assert (
        client.post(
            url("moderate", "post", post.pk),
            {"action": "publish", "revision": 1, "note": "staff is not a reviewer"},
        ).status_code
        == 403
    )
    assert (
        client.post(url("author_action", "post", post.pk, "withdraw"), {"revision": 1}).status_code
        == 404
    )
    assert client.get(url("author_blog", user.username)).status_code == 404
    monkeypatch.setattr("community.views.consume", lambda *args: False)
    client.force_login(user)
    assert (
        client.post(url("create"), {"kind": "blog", "title": "x", "body": "x"}).status_code == 429
    )
