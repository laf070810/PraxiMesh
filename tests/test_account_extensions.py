"""Account boundaries across private learning and opt-in community content."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from community.models import Appeal, Comment, ModerationEvent, Post, Report, State
from core.content import digest
from core.models import KnowledgeNode, Problem
from learning.models import Bookmark, Note, TutorialReading


def records(user, other_user):
    problem = Problem.objects.get(pk="sum-positive")
    tutorial = KnowledgeNode.objects.filter(kind="tutorial").first()
    Bookmark.objects.create(user=user, problem=problem)
    note = Note.objects.create(
        user=user, title="Private note", body="PRIVATE-NOTE-CONTENT", version=problem.current
    )
    TutorialReading.objects.create(
        user=user,
        tutorial=tutorial,
        completed_at=timezone.now(),
        content_digest=digest(tutorial.data),
    )
    post = Post.objects.create(
        author=user,
        kind="solution",
        title="My solution",
        body="MY-POST-CONTENT",
        version=problem.current,
        state=State.PUBLISHED,
    )
    Comment.objects.create(author=other_user, post=post, body="OTHER-COMMENT")
    Report.objects.create(reporter=other_user, post=post, reason="OTHER-PRIVATE-REPORT")
    event = ModerationEvent.objects.create(
        actor=user, target_kind="post", target_id=post.pk, target_revision=1, action="publish"
    )
    Note.objects.create(user=other_user, title="Other", body="OTHER-PRIVATE-NOTE")
    return note, post, event, tutorial


def test_account_export_contains_only_owned_extensions(catalog, logged_client, user, other_user):
    note, post, event, tutorial = records(user, other_user)
    response = logged_client.post(reverse("export_account"))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    data = response.json()
    assert data["learning"]["notes"][0]["body"] == note.body
    assert data["learning"]["reading"][0]["self_reported"] is True
    assert data["learning"]["bookmarks"][0]["problem"] == "sum-positive"
    assert data["community"]["posts"][0]["id"] == str(post.pk)
    assert data["community"]["moderation_events"][0]["action"] == event.action
    assert not data["community"]["comments"] and not data["community"]["reports"]
    for private_text in ("OTHER-COMMENT", "OTHER-PRIVATE-REPORT", "OTHER-PRIVATE-NOTE"):
        assert private_text not in response.content.decode()


def test_account_delete_removes_extensions_and_anonymizes_actor(
    catalog, logged_client, user, other_user
):
    note, post, event, tutorial = records(user, other_user)
    assert logged_client.post(reverse("delete_account"), {"confirm": "DELETE"}).status_code == 400
    assert Note.objects.filter(pk=note.pk).exists()
    response = logged_client.post(
        reverse("delete_account"), {"confirm": "DELETE", "password": "Test-passphrase-9621"}
    )
    assert response.status_code == 200
    assert not get_user_model().objects.filter(pk=user.pk).exists()
    for model in (Bookmark, TutorialReading, Post, Comment, Report):
        assert not model.objects.exists()
    assert list(Note.objects.values_list("user_id", flat=True)) == [other_user.pk]
    event.refresh_from_db()
    assert event.actor_id is None
    assert not hasattr(event, "body")
    assert get_user_model().objects.filter(pk=other_user.pk).exists()


def test_integrated_problem_and_tutorial_tools_do_not_expose_private_state(
    catalog, logged_client, user, other_user
):
    note, post, event, tutorial = records(user, other_user)
    page = logged_client.get(reverse("problem", args=["sum-positive"]))
    assert page.status_code == 200
    assert "取消收藏" in page.content.decode() and "写私人笔记" in page.content.decode()
    page = logged_client.get(reverse("node", args=[tutorial.pk]))
    assert "撤销已读标记" in page.content.decode()
    visitor = Client()
    for endpoint in (
        reverse("problem", args=["sum-positive"]),
        reverse("node", args=[tutorial.pk]),
    ):
        body = visitor.get(endpoint).content.decode()
        assert "取消收藏" not in body and "撤销已读标记" not in body
        assert "PRIVATE-NOTE-CONTENT" not in body


@pytest.mark.parametrize("endpoint", ["export_account", "delete_account"])
def test_account_extension_operations_keep_csrf_boundary(catalog, user, endpoint):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(reverse(endpoint)).status_code == 403


@pytest.mark.parametrize("state", [State.REJECTED, State.HIDDEN])
def test_own_comment_appeal_remains_reachable_after_parent_withdrawal(
    catalog, logged_client, user, other_user, state
):
    parent = Post.objects.create(
        author=other_user,
        kind="discussion",
        title="PRIVATE-PARENT-TITLE",
        body="PRIVATE-PARENT-BODY",
        state=State.WITHDRAWN,
    )
    comment = Comment.objects.create(
        author=user,
        post=parent,
        body="MY-COMMENT",
        state=state,
        moderation_note="PRIVATE-DECISION <script>never_execute()</script>",
    )
    Comment.objects.create(
        author=other_user,
        post=parent,
        body="OTHER-PRIVATE-COMMENT",
        state=State.REJECTED,
        moderation_note="OTHER-PRIVATE-DECISION",
    )
    assert (
        logged_client.get(reverse("community:comment_detail", args=[comment.pk])).status_code == 404
    )
    mine = logged_client.get(reverse("community:mine"), {"kind": "comment"})
    text = mine.content.decode()
    appeal_url = reverse("community:appeal_create", args=["comment", comment.pk])
    assert "MY-COMMENT" in text and "PRIVATE-DECISION" in text and appeal_url in text
    assert "&lt;script&gt;" in text and "<script>never_execute()" not in text
    for secret in [
        "PRIVATE-PARENT-TITLE",
        "PRIVATE-PARENT-BODY",
        "OTHER-PRIVATE-COMMENT",
        "OTHER-PRIVATE-DECISION",
    ]:
        assert secret not in text
    assert logged_client.get(appeal_url).status_code == 200
    assert (
        logged_client.post(
            appeal_url, {"revision": comment.revision, "reason": "Please review my comment"}
        ).status_code
        == 302
    )
    logged_client.force_login(other_user)
    other_page = logged_client.get(reverse("community:mine"), {"kind": "comment"}).content.decode()
    assert appeal_url not in other_page and "never_execute" not in other_page
    assert logged_client.get(appeal_url).status_code == 404


@pytest.mark.parametrize("parent_state", [State.DRAFT, State.WITHDRAWN])
def test_moderator_can_review_comment_without_access_to_private_parent(
    catalog, user, other_user, client, parent_state
):
    reviewer = get_user_model().objects.create_user(
        username="comment-reviewer", password="Reviewer-only-81642"
    )
    reviewer.user_permissions.add(
        Permission.objects.get(content_type__app_label="community", codename="moderate_content")
    )
    parent = Post.objects.create(
        author=other_user,
        kind="discussion",
        title="UNAVAILABLE-PARENT-TITLE",
        body="UNAVAILABLE-PARENT-BODY",
        state=parent_state,
    )
    comment = Comment.objects.create(
        author=user, post=parent, body="REVIEWABLE-COMMENT", state=State.PENDING
    )
    detail_url = reverse("community:comment_detail", args=[comment.pk])
    parent_url = reverse("community:detail", args=[parent.pk])
    assert client.get(detail_url).status_code == 404
    client.force_login(user)
    assert client.get(detail_url).status_code == 404
    client.force_login(reviewer)
    queue = client.get(reverse("community:moderation"), {"kind": "comment"})
    assert detail_url in queue.content.decode()
    page = client.get(detail_url)
    assert page.status_code == 200
    assert "REVIEWABLE-COMMENT" in page.content.decode()
    assert "父讨论当前不可查看" in page.content.decode()
    for secret in [parent_url, "UNAVAILABLE-PARENT-TITLE", "UNAVAILABLE-PARENT-BODY"]:
        assert secret not in page.content.decode()
    assert client.get(parent_url).status_code == 404
    moderation_url = reverse("community:moderate", args=["comment", comment.pk])
    assert (
        client.post(
            moderation_url,
            {"action": "publish", "revision": 1, "note": "Cannot bypass parent gate"},
        ).status_code
        == 409
    )
    comment.refresh_from_db()
    assert comment.state == State.PENDING
    assert (
        client.post(
            moderation_url, {"action": "reject", "revision": 1, "note": "Needs revision"}
        ).status_code
        == 302
    )
    comment.refresh_from_db()
    assert comment.state == State.REJECTED

    client.force_login(user)
    appeal_url = reverse("community:appeal_create", args=["comment", comment.pk])
    assert (
        client.post(
            appeal_url, {"revision": 1, "reason": "Please reconsider my comment"}
        ).status_code
        == 302
    )
    appeal = Appeal.objects.get(comment=comment)
    client.force_login(reviewer)
    case = client.get(reverse("community:appeal_detail", args=[appeal.pk]))
    assert detail_url in case.content.decode() and client.get(detail_url).status_code == 200
    assert (
        client.post(
            reverse("community:resolve_case", args=["appeal", appeal.pk]),
            {"decision": "reopened", "note": "Review text again"},
        ).status_code
        == 302
    )
    assert (
        client.post(
            moderation_url, {"action": "publish", "revision": 1, "note": "Still cannot publish"}
        ).status_code
        == 409
    )
    # A comment that was public before its parent disappeared can still be hidden.
    Comment.objects.filter(pk=comment.pk).update(state=State.PUBLISHED)
    assert (
        client.post(
            moderation_url,
            {"action": "hide", "revision": 1, "note": "Hide this earlier public comment"},
        ).status_code
        == 302
    )
    comment.refresh_from_db()
    assert comment.state == State.HIDDEN and client.get(detail_url).status_code == 200
    # Unsubmitted/withdrawn comment bodies remain private even to this reviewer.
    for state in [State.DRAFT, State.WITHDRAWN]:
        Comment.objects.filter(pk=comment.pk).update(state=state)
        assert client.get(detail_url).status_code == 404
