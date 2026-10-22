"""Community permissions, real database state transitions, and escaped HTTP flows."""

import io
import json

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse

from community import services
from community.models import Appeal, Comment, ModerationEvent, Post, Report, State
from core.models import AccountProfile, Problem, Submission

pytestmark = pytest.mark.django_db


@pytest.fixture
def moderator(django_user_model):
    user = django_user_model.objects.create_user(
        username="reviewer", password="Review-password-8345"
    )
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="community", codename="moderate_content")
    )
    return user


@pytest.fixture
def post(user):
    return Post.objects.create(
        author=user, kind=Post.Kind.DISCUSSION, title="Private title", body="Private body"
    )


def approve(post, moderator):
    services.author_action(post.author, "post", post.pk, "submit", post.revision)
    return services.moderate(moderator, "post", post.pk, "publish", post.revision, "符合讨论规范。")


def url(name, *args):
    return reverse("community:" + name, args=args)


def test_draft_pending_and_published_visibility(post, client, user, other_user, moderator):
    for reader in (other_user, moderator):
        client.force_login(reader)
        assert client.get(url("detail", post.pk)).status_code == 404
    client.force_login(user)
    assert client.get(url("detail", post.pk)).status_code == 200
    assert (
        client.post(url("author_action", "post", post.pk, "submit"), {"revision": 1}).status_code
        == 302
    )
    post.refresh_from_db()
    assert post.state == State.PENDING
    client.force_login(other_user)
    assert client.get(url("detail", post.pk)).status_code == 404
    client.force_login(moderator)
    assert client.get(url("detail", post.pk)).status_code == 200
    response = client.post(
        url("moderate", "post", post.pk), {"action": "publish", "revision": 1, "note": "已审核"}
    )
    assert response.status_code == 302
    client.logout()
    assert b"Private body" in client.get(url("detail", post.pk)).content
    assert b"Private title" in client.get(url("index")).content
    assert ModerationEvent.objects.get().action == "publish"


def test_solution_explicit_version_and_no_private_submission_import(catalog, logged_client, user):
    version = Problem.objects.get(pk="sum-positive").current
    Submission.objects.create(
        user=user,
        version=version,
        source="NEVER_COPY_PRIVATE_SOURCE",
        source_digest="a" * 64,
        mode="submit",
    )
    response = logged_client.post(
        url("create"), {"kind": "solution", "title": "My solution", "body": "Manual explanation"}
    )
    assert response.status_code == 400 and not Post.objects.exists()
    response = logged_client.post(
        url("create"),
        {
            "kind": "solution",
            "title": "My solution",
            "body": "Manual explanation",
            "version": version.pk,
            "submission": Submission.objects.get().pk,
        },
    )
    assert response.status_code == 302
    item = Post.objects.get()
    assert item.version_id == version.pk and item.state == State.DRAFT
    assert item.body == "Manual explanation"
    assert b"NEVER_COPY_PRIVATE_SOURCE" not in logged_client.get(url("detail", item.pk)).content


def test_public_writing_escapes_html_and_uses_username_not_private_display_name(
    user, moderator, client
):
    AccountProfile.objects.create(
        user=user, display_name="PRIVATE_PROFILE_NAME", recovery_hash="test"
    )
    item = Post.objects.create(
        author=user,
        kind="question",
        title='<img src=x onerror="alert(1)">',
        body='<script>alert("xss")</script>',
    )
    approve(item, moderator)
    response = client.get(url("detail", item.pk))
    assert response.status_code == 200
    assert b"<script>alert" not in response.content and b"&lt;script&gt;" in response.content
    assert b"<img src=x" not in response.content and b"&lt;img" in response.content
    assert user.username.encode() in response.content
    assert b"PRIVATE_PROFILE_NAME" not in response.content


def test_edit_published_content_immediately_hides_and_stale_review_cannot_publish(
    post, moderator, logged_client, client
):
    approve(post, moderator)
    response = logged_client.post(
        url("edit", post.pk),
        {
            "kind": "discussion",
            "title": "Edited title",
            "body": "Edited private text",
            "revision": 1,
        },
    )
    assert response.status_code == 302
    post.refresh_from_db()
    assert post.state == State.PENDING and post.revision == 2
    client.logout()
    assert client.get(url("detail", post.pk)).status_code == 404
    client.force_login(moderator)
    stale = client.post(
        url("moderate", "post", post.pk),
        {"action": "publish", "revision": 1, "note": "reviewed old version"},
    )
    assert stale.status_code == 409
    assert (
        client.post(
            url("moderate", "post", post.pk),
            {"action": "publish", "revision": 2, "note": "reviewed new version"},
        ).status_code
        == 302
    )


def test_stale_author_edit_does_not_overwrite_new_text(post, logged_client):
    data = {"kind": "discussion", "title": "new", "body": "first revision", "revision": 1}
    assert logged_client.post(url("edit", post.pk), data).status_code == 302
    data["body"] = "stale overwrite"
    assert logged_client.post(url("edit", post.pk), data).status_code == 409
    post.refresh_from_db()
    assert post.body == "first revision"


@pytest.mark.parametrize("action", ["submit", "withdraw", "delete"])
def test_cross_account_author_actions_denied(post, client, other_user, action):
    client.force_login(other_user)
    assert (
        client.post(url("author_action", "post", post.pk, action), {"revision": 1}).status_code
        == 404
    )
    assert client.get(url("edit", post.pk)).status_code == 404
    assert Post.objects.filter(pk=post.pk).exists()


def test_staff_without_permission_cannot_read_queue_or_moderate(post, other_user, client):
    services.author_action(post.author, "post", post.pk, "submit", 1)
    other_user.is_staff = True
    other_user.save()
    client.force_login(other_user)
    assert client.get(url("moderation")).status_code == 403
    assert client.get(url("detail", post.pk)).status_code == 404
    assert (
        client.post(
            url("moderate", "post", post.pk),
            {"action": "publish", "revision": 1, "note": "no permission"},
        ).status_code
        == 403
    )


def test_moderator_cannot_review_own_writing(moderator, client):
    post = Post.objects.create(
        author=moderator, kind="discussion", title="Own", body="own", state=State.PENDING
    )
    client.force_login(moderator)
    assert (
        client.post(
            url("moderate", "post", post.pk),
            {"action": "publish", "revision": 1, "note": "self review"},
        ).status_code
        == 403
    )
    assert not ModerationEvent.objects.exists()


def test_comments_require_explicit_submission_and_moderation(post, moderator, other_user, client):
    approve(post, moderator)
    client.force_login(other_user)
    response = client.post(url("comment_create", post.pk), {"body": "PRIVATE_COMMENT"})
    assert response.status_code == 302
    comment = Comment.objects.get()
    assert comment.state == State.DRAFT
    client.force_login(post.author)
    assert b"PRIVATE_COMMENT" not in client.get(url("detail", post.pk)).content
    assert client.get(url("comment_detail", comment.pk)).status_code == 404
    client.force_login(other_user)
    assert (
        client.post(
            url("author_action", "comment", comment.pk, "submit"), {"revision": 1}
        ).status_code
        == 302
    )
    client.force_login(moderator)
    assert (
        client.post(
            url("moderate", "comment", comment.pk),
            {"action": "publish", "revision": 1, "note": "valid comment"},
        ).status_code
        == 302
    )
    client.logout()
    assert b"PRIVATE_COMMENT" in client.get(url("detail", post.pk)).content


@pytest.mark.parametrize("state", [State.HIDDEN, State.WITHDRAWN, State.DRAFT, State.PENDING])
def test_comment_and_report_routes_do_not_bypass_hidden_parent(
    post, moderator, other_user, client, state
):
    approve(post, moderator)
    comment = Comment.objects.create(
        post=post, author=other_user, body="CHILD_PRIVATE_MARKER", state=State.PUBLISHED
    )
    post.state = state
    post.save()
    client.logout()
    assert client.get(url("comment_detail", comment.pk)).status_code == 404
    client.force_login(other_user)
    assert client.get(url("comment_detail", comment.pk)).status_code == 404
    assert client.get(url("report_create", "comment", comment.pk)).status_code == 404
    assert (
        client.post(
            url("report_create", "comment", comment.pk), {"reason": "hidden parent"}
        ).status_code
        == 404
    )
    assert not Report.objects.exists()
    # Own text remains exportable and removable without access to the parent's body.
    assert services.export_user_data(other_user)["comments"][0]["body"] == "CHILD_PRIVATE_MARKER"
    assert (
        client.post(
            url("author_action", "comment", comment.pk, "delete"), {"revision": 1}
        ).status_code
        == 302
    )


def test_withdrawn_problem_hides_bound_posts_and_comments(
    catalog, user, other_user, moderator, client
):
    problem = Problem.objects.get(pk="sum-positive")
    post = Post.objects.create(
        author=user,
        kind="solution",
        title="Bound solution",
        body="Bound answer",
        version=problem.current,
    )
    approve(post, moderator)
    comment = Comment.objects.create(
        post=post, author=other_user, body="bound comment", state=State.PUBLISHED
    )
    problem.published = False
    problem.save()
    assert b"Bound solution" not in client.get(url("index")).content
    assert client.get(url("detail", post.pk)).status_code == 404
    assert client.get(url("comment_detail", comment.pk)).status_code == 404
    for reader in (user, moderator):
        client.force_login(reader)
        assert client.get(url("detail", post.pk)).status_code == 200
    services.moderate(moderator, "post", post.pk, "hide", 1, "题目下线")
    services.author_action(user, "post", post.pk, "withdraw", 1)
    with pytest.raises(services.CommunityError):
        services.author_action(user, "post", post.pk, "submit", 1)


def test_reports_are_private_and_subject_author_cannot_inspect_or_resolve(
    post, moderator, other_user, client
):
    approve(post, moderator)
    client.force_login(other_user)
    assert (
        client.post(
            url("report_create", "post", post.pk), {"reason": "PRIVATE_REPORT_REASON"}
        ).status_code
        == 302
    )
    report = Report.objects.get()
    assert client.get(url("report_detail", report.pk)).status_code == 200
    client.force_login(post.author)
    assert client.get(url("report_detail", report.pk)).status_code == 404
    assert b"PRIVATE_REPORT_REASON" not in client.get(url("detail", post.pk)).content
    client.force_login(moderator)
    assert client.get(url("report_detail", report.pk)).status_code == 200
    assert (
        client.post(
            url("resolve_case", "report", report.pk),
            {"decision": "resolved", "note": "已核对并处理"},
        ).status_code
        == 302
    )
    report.refresh_from_db()
    assert report.status == Report.Status.RESOLVED
    assert (
        client.post(
            url("resolve_case", "report", report.pk), {"decision": "dismissed", "note": "again"}
        ).status_code
        == 409
    )
    assert not services.export_user_data(post.author)["reports"]


def test_reporting_hidden_or_pending_content_is_denied(post, moderator, client, other_user):
    services.author_action(post.author, "post", post.pk, "submit", 1)
    for reader in (other_user, moderator):
        client.force_login(reader)
        assert (
            client.post(url("report_create", "post", post.pk), {"reason": "not public"}).status_code
            == 404
        )
    assert not Report.objects.exists()


def test_duplicate_open_report_is_rejected(post, other_user, moderator):
    approve(post, moderator)
    services.create_report(other_user, "post", post.pk, "first")
    with pytest.raises(services.CommunityError):
        services.create_report(other_user, "post", post.pk, "duplicate")
    assert Report.objects.count() == 1


def test_appeal_private_workflow_reopens_for_fresh_review(post, moderator, other_user, client):
    services.author_action(post.author, "post", post.pk, "submit", 1)
    services.moderate(moderator, "post", post.pk, "reject", 1, "需要补充解释")
    client.force_login(post.author)
    assert (
        client.post(
            url("appeal_create", "post", post.pk), {"reason": "PRIVATE_APPEAL", "revision": 1}
        ).status_code
        == 302
    )
    appeal = Appeal.objects.get()
    client.force_login(other_user)
    assert client.get(url("appeal_detail", appeal.pk)).status_code == 404
    assert (
        client.post(
            url("appeal_create", "post", post.pk), {"reason": "not mine", "revision": 1}
        ).status_code
        == 404
    )
    client.force_login(moderator)
    assert (
        client.post(
            url("resolve_case", "appeal", appeal.pk), {"decision": "reopened", "note": "重新核对"}
        ).status_code
        == 302
    )
    post.refresh_from_db()
    appeal.refresh_from_db()
    assert post.state == State.PENDING and appeal.status == Appeal.Status.REOPENED
    client.logout()
    assert client.get(url("detail", post.pk)).status_code == 404
    assert len(services.export_user_data(post.author)["appeals"]) == 1


def test_edit_supersedes_pending_appeal_and_old_decision_cannot_publish(post, moderator):
    services.author_action(post.author, "post", post.pk, "submit", 1)
    services.moderate(moderator, "post", post.pk, "reject", 1, "补充")
    appeal = services.create_appeal(post.author, "post", post.pk, "请复核", 1)
    services.save_post(
        post.author,
        {"kind": "discussion", "title": "revised", "body": "new revision", "version": None},
        pk=post.pk,
        revision=1,
    )
    appeal.refresh_from_db()
    assert appeal.status == Appeal.Status.SUPERSEDED
    with pytest.raises(services.CommunityError):
        services.resolve_appeal(moderator, appeal.pk, "reopened", "old")


def test_moderators_cannot_resolve_their_own_report_or_appeal(post, moderator, other_user):
    approve(post, moderator)
    report = services.create_report(moderator, "post", post.pk, "own report")
    with pytest.raises(PermissionDenied):
        services.resolve_report(moderator, report.pk, "resolved", "self review")
    own = Post.objects.create(
        author=moderator, kind="discussion", title="own", body="own", state=State.REJECTED
    )
    appeal = services.create_appeal(moderator, "post", own.pk, "own appeal", 1)
    with pytest.raises(PermissionDenied):
        services.resolve_appeal(moderator, appeal.pk, "reopened", "self review")


def test_account_deletion_cascades_text_and_private_cases_but_keeps_minimal_events(
    post, moderator, other_user
):
    approve(post, moderator)
    Comment.objects.create(
        post=post, author=other_user, body="other user's reply", state=State.PUBLISHED
    )
    services.create_report(other_user, "post", post.pk, "private report")
    services.moderate(moderator, "post", post.pk, "hide", 1, "private moderation reason")
    services.create_appeal(post.author, "post", post.pk, "private appeal", 1)
    post.author.delete()
    assert not Post.objects.exists() and not Comment.objects.exists()
    assert not Report.objects.exists() and not Appeal.objects.exists()
    assert ModerationEvent.objects.count() == 2
    moderator.delete()
    assert not ModerationEvent.objects.exclude(actor=None).exists()
    fields = {field.name for field in ModerationEvent._meta.fields}
    assert fields == {
        "id",
        "actor",
        "target_kind",
        "target_id",
        "target_revision",
        "action",
        "created_at",
    }


def test_reporter_account_deletion_removes_private_report_without_deleting_target(
    post, moderator, other_user
):
    approve(post, moderator)
    services.create_report(other_user, "post", post.pk, "private report")
    other_user.delete()
    assert not Report.objects.exists() and Post.objects.filter(pk=post.pk).exists()


def test_export_is_json_serializable_and_excludes_other_users_private_cases(
    post, moderator, other_user
):
    approve(post, moderator)
    services.create_report(other_user, "post", post.pk, "OTHER_REPORT_SECRET")
    own = services.export_user_data(post.author)
    encoded = json.dumps(own)
    assert "OTHER_REPORT_SECRET" not in encoded
    assert str(post.pk) in encoded and post.body in encoded
    assert own["posts"][0]["created_at"]


@pytest.mark.parametrize(
    "field,value",
    [("title", "x" * 161), ("body", "x" * 20001), ("kind", "unknown"), ("body", "   ")],
)
def test_post_lengths_and_kind_are_validated(logged_client, field, value):
    data = {"title": "valid", "body": "valid", "kind": "discussion"}
    data[field] = value
    assert logged_client.post(url("create"), data).status_code == 400
    assert not Post.objects.exists()


def test_comment_and_case_lengths_are_bounded(post, moderator, other_user, client):
    approve(post, moderator)
    client.force_login(other_user)
    assert client.post(url("comment_create", post.pk), {"body": "x" * 5001}).status_code == 400
    assert (
        client.post(url("report_create", "post", post.pk), {"reason": "x" * 2001}).status_code
        == 400
    )
    assert not Comment.objects.exists() and not Report.objects.exists()


def test_post_csrf_authentication_and_rate_limits(post, user, client, logged_client, monkeypatch):
    client.logout()
    assert client.post(url("create"), {}).status_code == 302
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert (
        csrf.post(url("author_action", "post", post.pk, "submit"), {"revision": 1}).status_code
        == 403
    )
    logged_client.force_login(user)
    assert logged_client.get(url("author_action", "post", post.pk, "submit")).status_code == 405
    assert logged_client.get(url("moderate", "post", post.pk)).status_code == 405
    from community import views

    monkeypatch.setattr(views, "consume", lambda *args: False)
    assert (
        logged_client.post(
            url("create"), {"title": "valid", "body": "valid", "kind": "discussion"}
        ).status_code
        == 429
    )
    assert Post.objects.count() == 1


def test_lists_paginate_and_never_include_other_private_posts(user, other_user, client):
    Post.objects.bulk_create(
        [
            Post(
                author=user,
                kind="discussion",
                title=f"Public {i}",
                body="body",
                state=State.PUBLISHED,
            )
            for i in range(23)
        ]
    )
    Post.objects.create(
        author=other_user, kind="discussion", title="PRIVATE_OUTSIDER", body="private"
    )
    response = client.get(url("index"))
    assert len(response.context["page"]) == 20
    assert len(client.get(url("index"), {"page": 2}).context["page"]) == 3
    assert b"PRIVATE_OUTSIDER" not in response.content
    client.force_login(user)
    assert len(client.get(url("mine")).context["page"]) == 20
    assert client.get(url("mine"))["Cache-Control"] == "no-store"


def test_moderator_management_command_grants_only_explicit_permission(user):
    output = io.StringIO()
    call_command("community_moderator", user.username, "--grant", stdout=output)
    user.refresh_from_db()
    assert list(user.user_permissions.values_list("codename", flat=True)) == ["moderate_content"]
    assert not user.is_staff and not user.is_superuser
    call_command("community_moderator", user.username, "--revoke", stdout=output)
    assert not user.user_permissions.exists()
    with pytest.raises(CommandError):
        call_command("community_moderator", "missing-account", "--grant", stdout=output)
    with pytest.raises(CommandError):
        call_command("community_moderator", user.username, stdout=output)


def test_problem_entry_filters_and_preselects_only_published_versions(catalog, user, logged_client):
    version = Problem.objects.get(pk="sum-positive").current
    other = Problem.objects.get(pk="unique-stable").current
    Post.objects.create(
        author=user,
        kind="solution",
        title="Bound solution",
        body="body",
        version=version,
        state=State.PUBLISHED,
    )
    Post.objects.create(
        author=user,
        kind="solution",
        title="Different solution",
        body="body",
        version=other,
        state=State.PUBLISHED,
    )
    response = logged_client.get(url("index"), {"problem": version.problem_id})
    assert b"Bound solution" in response.content and b"Different solution" not in response.content
    response = logged_client.get(url("create"), {"kind": "solution", "version": version.pk})
    assert response.status_code == 200
    assert response.context["form"].initial["kind"] == "solution"
    assert response.context["form"].initial["version"].pk == version.pk
    version.problem.published = False
    version.problem.save()
    assert logged_client.get(url("index"), {"problem": version.problem_id}).status_code == 404
    assert (
        logged_client.get(url("create"), {"kind": "solution", "version": version.pk}).status_code
        == 400
    )


@pytest.mark.parametrize(
    "query",
    [
        {"kind": "unreviewed"},
        {"version": "not-an-id"},
        {"version": "999999999999999999999999999999"},
    ],
)
def test_create_prefill_rejects_invalid_kind_or_version(logged_client, query):
    assert logged_client.get(url("create"), query).status_code == 400
    assert not Post.objects.exists()
