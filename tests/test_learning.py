import copy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.content import digest
from core.models import KnowledgeNode, Problem, ProblemVersion, Submission
from learning.models import Bookmark, Note, TutorialReading
from learning.services import export_user_data


def url(name, *args):
    return reverse(f"learning:{name}", args=args)


def current(slug="sum-positive"):
    return Problem.objects.select_related("current").get(pk=slug).current


def next_version(version):
    data = copy.deepcopy(version.data)
    data["revision"] += 1
    data["statement"] = "已更新的题意"
    result = ProblemVersion.objects.create(
        problem=version.problem, revision=data["revision"], digest=digest(data), data=data
    )
    Problem.objects.filter(pk=version.problem_id).update(current=result)
    return result


def job(user, version=None, verdict="accepted", *, mode="submit", status="finished", at=None):
    return Submission.objects.create(
        user=user,
        version=version or current(),
        source="PRIVATE SUBMISSION SOURCE",
        source_digest="a" * 64,
        mode=mode,
        status=status,
        verdict=verdict,
        finished_at=at or timezone.now(),
    )


@pytest.mark.parametrize("name", ["index", "bookmarks", "notes", "note_create", "reading"])
def test_private_learning_requires_login(catalog, client, name):
    assert client.get(url(name)).status_code == 302


def test_bookmark_is_idempotent_private_and_follows_stable_problem(
    catalog, logged_client, user, other_user
):
    endpoint = url("bookmark", "sum-positive")
    assert (
        logged_client.post(endpoint, {"action": "save", "user": other_user.pk}).status_code == 302
    )
    assert logged_client.post(endpoint, {"action": "save"}).status_code == 302
    assert Bookmark.objects.get().user == user
    changed = next_version(current())
    response = logged_client.get(url("bookmarks"))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert response.context["page"][0].problem.current_id == changed.pk
    logged_client.force_login(other_user)
    assert not list(logged_client.get(url("bookmarks")).context["page"])
    logged_client.post(endpoint, {"action": "remove"})
    assert Bookmark.objects.filter(user=user).count() == 1


def test_bookmark_withdrawal_keeps_private_record_but_prevents_new_save(
    catalog, logged_client, user
):
    endpoint = url("bookmark", "sum-positive")
    logged_client.post(endpoint, {"action": "save"})
    Problem.objects.filter(pk="sum-positive").update(published=False)
    page = logged_client.get(url("bookmarks"))
    assert "已下线" in page.content.decode()
    assert "/problems/sum-positive/" not in page.content.decode()
    assert logged_client.post(endpoint, {"action": "save"}).status_code == 404
    assert logged_client.post(endpoint, {"action": "toggle"}).status_code == 400
    assert logged_client.post(endpoint, {"action": "remove"}).status_code == 302
    assert not Bookmark.objects.filter(user=user).exists()


def test_notes_create_edit_and_read_plain_text_with_owned_version(
    catalog, logged_client, user, other_user
):
    version = current()
    body = '<script>alert("private")</script>\nMy explanation'
    response = logged_client.post(
        url("note_create"),
        {"title": "Private idea", "body": body, "version": version.pk, "user": other_user.pk},
    )
    assert response.status_code == 302
    note = Note.objects.get()
    assert note.user == user and note.version == version and note.body == body
    page = logged_client.get(response.url)
    assert page["Cache-Control"] == "no-store"
    assert '<script>alert("private")</script>' not in page.content.decode()
    assert "&lt;script&gt;" in page.content.decode()
    assert "PRIVATE SUBMISSION SOURCE" not in page.content.decode()
    assert (
        logged_client.post(
            url("note_edit", note.pk),
            {"title": "Revised", "body": "New body", "version": version.pk},
        ).status_code
        == 302
    )
    note.refresh_from_db()
    assert note.body == "New body" and note.title == "Revised"
    assert logged_client.post(url("note_delete", note.pk)).status_code == 302
    assert not Note.objects.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": " "},
        {"title": "x" * 121},
        {"body": " "},
        {"body": "x" * 10001},
        {"body": "bad\0text"},
        {"version": "not-a-number"},
        {"node": "missing-node"},
    ],
)
def test_note_validates_real_post_without_partial_save(catalog, logged_client, overrides):
    response = logged_client.post(
        url("note_create"), {"title": "Title", "body": "Body", **overrides}
    )
    assert response.status_code == 400
    assert not Note.objects.exists()


def test_note_targets_are_exclusive_and_db_enforces_it(catalog, logged_client, user):
    version = current()
    response = logged_client.post(
        url("note_create"),
        {"title": "Title", "body": "Body", "version": version.pk, "node": "functions"},
    )
    assert response.status_code == 400 and not Note.objects.exists()
    with pytest.raises(IntegrityError), transaction.atomic():
        Note.objects.create(
            user=user, title="Title", body="Body", version=version, node_id="functions"
        )


def test_note_cross_account_read_edit_delete_and_list_are_denied(
    catalog, logged_client, user, other_user
):
    note = Note.objects.create(
        user=other_user, title="Other private title", body="Other private body"
    )
    assert logged_client.get(url("note_detail", note.pk)).status_code == 404
    assert logged_client.get(url("note_edit", note.pk)).status_code == 404
    assert (
        logged_client.post(
            url("note_edit", note.pk), {"title": "stolen", "body": "stolen"}
        ).status_code
        == 404
    )
    assert logged_client.post(url("note_delete", note.pk)).status_code == 404
    assert "Other private" not in logged_client.get(url("notes")).content.decode()
    note.refresh_from_db()
    assert note.user == other_user and note.body == "Other private body"


def test_note_old_and_withdrawn_targets_can_be_retained_but_not_forged(
    catalog, logged_client, user
):
    original = current()
    note = Note.objects.create(user=user, title="Original", body="My idea", version=original)
    next_version(original)
    data = {"title": "Updated note", "body": "Still about original", "version": original.pk}
    assert logged_client.post(url("note_create"), data).status_code == 400
    assert logged_client.post(url("note_edit", note.pk), data).status_code == 302
    text = logged_client.get(url("note_detail", note.pk)).content.decode()
    assert "笔记关联旧版本" in text and original.data["statement"] in text
    assert "已更新的题意" not in text
    Problem.objects.filter(pk=original.problem_id).update(published=False)
    assert logged_client.post(url("note_edit", note.pk), data).status_code == 302
    assert "题目已下线" in logged_client.get(url("note_detail", note.pk)).content.decode()
    KnowledgeNode.objects.filter(pk="functions").update(published=False)
    assert (
        logged_client.post(
            url("note_create"), {"title": "No", "body": "No", "node": "functions"}
        ).status_code
        == 400
    )
    note.refresh_from_db()
    assert note.version_id == original.pk


def test_note_can_link_published_knowledge_or_remain_independent(catalog, logged_client, user):
    for extra in ({"node": "functions"}, {}):
        assert (
            logged_client.post(url("note_create"), {"title": "A", "body": "B", **extra}).status_code
            == 302
        )
    assert Note.objects.filter(user=user, node_id="functions").count() == 1
    assert Note.objects.filter(user=user, node=None, version=None).count() == 1


def test_reading_is_private_self_reported_and_detects_content_updates(
    catalog, logged_client, user, other_user
):
    endpoint = url("reading_mark", "tutorial-functions")
    assert (
        logged_client.post(endpoint, {"action": "complete", "user": other_user.pk}).status_code
        == 302
    )
    reading = TutorialReading.objects.get()
    assert reading.user == user
    assert "自述已读" in logged_client.get(url("reading")).content.decode()
    node = reading.tutorial
    node.data = {**node.data, "body": node.data["body"] + "\nNew content."}
    node.save()
    assert "教程内容已更新" in logged_client.get(url("reading")).content.decode()
    logged_client.force_login(other_user)
    assert "自述已读" not in logged_client.get(url("reading")).content.decode()
    logged_client.post(endpoint, {"action": "reset"})
    assert TutorialReading.objects.filter(user=user).exists()
    logged_client.force_login(user)
    logged_client.post(endpoint, {"action": "complete"})
    assert "教程内容已更新" not in logged_client.get(url("reading")).content.decode()
    assert logged_client.get(url("index")).context["summary"]["completed"] == 0


def test_reading_rejects_non_tutorial_and_withdrawal_allows_reset(catalog, logged_client, user):
    assert (
        logged_client.post(url("reading_mark", "functions"), {"action": "complete"}).status_code
        == 404
    )
    endpoint = url("reading_mark", "tutorial-functions")
    assert logged_client.post(endpoint, {"action": "unknown"}).status_code == 400
    logged_client.post(endpoint, {"action": "complete"})
    KnowledgeNode.objects.filter(pk="tutorial-functions").update(published=False)
    assert "已下线" in logged_client.get(url("reading")).content.decode()
    assert logged_client.post(endpoint, {"action": "complete"}).status_code == 404
    assert logged_client.post(endpoint, {"action": "reset"}).status_code == 302
    assert not TutorialReading.objects.filter(user=user).exists()


def test_every_write_is_post_only_and_csrf_protected(catalog, user):
    note = Note.objects.create(user=user, title="Private", body="Body")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    endpoints = [
        url("bookmark", "sum-positive"),
        url("note_create"),
        url("note_edit", note.pk),
        url("note_delete", note.pk),
        url("reading_mark", "tutorial-functions"),
    ]
    for endpoint in endpoints:
        assert (
            client.post(endpoint, {"action": "complete", "title": "No", "body": "No"}).status_code
            == 403
        )
    for endpoint in [endpoints[0], endpoints[3], endpoints[4]]:
        assert client.get(endpoint).status_code == 405
    client.logout()
    # Authentication also protects POST when CSRF middleware has a valid token.
    ordinary = Client()
    for endpoint in endpoints:
        assert ordinary.post(endpoint, {}).status_code == 302


def test_write_limit_is_shared_across_learning_actions(catalog, logged_client, user):
    endpoint = url("bookmark", "sum-positive")
    with patch("core.limits.time.time", return_value=1800000000):
        for _ in range(60):
            assert logged_client.post(endpoint, {"action": "save"}).status_code == 302
        denied = logged_client.post(url("note_create"), {"title": "Blocked", "body": "No save"})
        assert denied.status_code == 429 and denied["Retry-After"] == "60"
        assert (
            logged_client.post(
                url("reading_mark", "tutorial-functions"), {"action": "complete"}
            ).status_code
            == 429
        )
    assert Bookmark.objects.filter(user=user).count() == 1
    assert not Note.objects.exists() and not TutorialReading.objects.exists()


def test_evidence_counts_only_own_finished_formal_results(catalog, logged_client, user, other_user):
    accepted = job(user)
    failed = job(user, verdict="wrong_answer")
    for excluded in [
        job(other_user, verdict="syntax_error"),
        job(user, verdict="memory_limit", mode="run"),
        job(user, verdict="accepted", mode="run"),
        job(user, verdict="internal_error"),
        job(user, verdict="unknown-verdict"),
        job(user, status="queued"),
        job(user, verdict="runtime_error", status="running"),
    ]:
        assert excluded.pk
    response = logged_client.get(url("index"))
    summary = response.context["summary"]
    assert summary["attempts"] == 2 and summary["accepted_attempts"] == 1
    assert summary["completed"] == 1 and summary["attempted"] == 1
    assert summary["recent_errors"] == [
        {"verdict": "wrong_answer", "label": "答案有误", "count": 1}
    ]
    assert [row["id"] for row in response.context["recent_errors"]] == [failed.pk]
    text = response.content.decode()
    assert str(accepted.pk) not in text and "PRIVATE SUBMISSION SOURCE" not in text
    assert "一次通过不等于掌握" in text


def test_current_evidence_excludes_previous_versions_and_withdrawn_problems(
    catalog, logged_client, user
):
    original = current()
    job(user, original)
    latest = next_version(original)
    wrong = job(user, latest, "syntax_error")
    response = logged_client.get(url("index"))
    assert response.context["summary"]["completed"] == 0
    assert response.context["summary"]["attempts"] == 1
    assert response.context["archived_attempts"] == 1
    assert [row["id"] for row in response.context["recent_errors"]] == [wrong.pk]
    Problem.objects.filter(pk=original.problem_id).update(published=False)
    response = logged_client.get(url("index"))
    assert response.context["summary"]["attempts"] == 0
    assert response.context["archived_attempts"] == 2
    assert not response.context["recent_errors"]


def test_domain_deduplicates_multi_concept_problem_and_shows_repetition(
    catalog, logged_client, user
):
    job(user)
    job(user)
    response = logged_client.get(url("index"))
    domain = next(row for row in response.context["domains"] if row["id"] == "programming")
    assert domain["completed"] == 1 and domain["attempted"] == 1
    assert domain["attempts"] == 2 and domain["accepted_attempts"] == 2
    assert sum(item["count"] for item in domain["difficulty_evidence"]) == 1
    for concept in response.context["concepts"]:
        if concept["id"] in current().data["concepts"]:
            assert concept["completed"] == 1 and concept["attempts"] == 2


def test_recent_errors_and_review_do_not_blame_platform_failures(catalog, logged_client, user):
    now = timezone.now()
    old = job(user, verdict="wrong_answer", at=now - timedelta(days=31))
    wrong = job(user, verdict="syntax_error", at=now - timedelta(hours=1))
    job(user, verdict="internal_error", at=now)
    response = logged_client.get(url("index"))
    assert response.context["summary"]["attempts"] == 2
    assert [item["id"] for item in response.context["recent_errors"]] == [wrong.pk]
    recommendation = next(
        row
        for row in response.context["recommendations"].paginator.object_list
        if row["problem"]["id"] == "sum-positive"
    )
    assert recommendation["kind"] == "复习" and "语法错误" in recommendation["reason"]
    assert str(old.pk) not in response.content.decode()


def test_spaced_review_and_prerequisite_suggestions_explain_their_basis(
    catalog, logged_client, user
):
    job(user, at=timezone.now() - timedelta(days=31))
    # Running public examples cannot reset the formal-practice review date.
    job(user, mode="run")
    response = logged_client.get(url("index"))
    recommendations = response.context["recommendations"].paginator.object_list
    review = next(row for row in recommendations if row["problem"]["id"] == "sum-positive")
    assert review["kind"] == "间隔复习" and "超过 30 天" in review["reason"]
    job(user, verdict="accepted")
    recommendations = (
        logged_client.get(url("index")).context["recommendations"].paginator.object_list
    )
    assert not any(row["problem"]["id"] == "sum-positive" for row in recommendations)
    assert any(row["kind"] == "前置练习" and "可以跳过" in row["reason"] for row in recommendations)


def test_private_collection_pagination_is_stable_and_complete(catalog, logged_client, user):
    original = current()
    for index in range(21):
        problem = Problem.objects.create(pk=f"learning-page-{index:02d}")
        data = {**original.data, "id": problem.pk, "title": f"Exercise {index:02d}"}
        version = ProblemVersion.objects.create(
            problem=problem, revision=1, digest=digest(data), data=data
        )
        problem.current = version
        problem.save()
        Bookmark.objects.create(user=user, problem=problem)
        Note.objects.create(user=user, title=f"Note {index:02d}", body="Private")
    for name in ["bookmarks", "notes"]:
        first = logged_client.get(url(name)).context["page"]
        second = logged_client.get(url(name), {"page": 2}).context["page"]
        assert len(first) == 20 and len(second) == 1
        assert {item.pk for item in first}.isdisjoint({item.pk for item in second})
        assert b"?page=2" in logged_client.get(url(name)).content
        assert logged_client.get(url(name), {"page": "bad"}).status_code == 200


def test_export_is_private_json_ready_and_account_deletion_cascades(
    catalog, logged_client, user, other_user
):
    import json

    for owner in [user, other_user]:
        Bookmark.objects.create(user=owner, problem=current().problem)
        Note.objects.create(
            user=owner, title=owner.username, body="Private note", version=current()
        )
        TutorialReading.objects.create(
            user=owner,
            tutorial_id="tutorial-functions",
            completed_at=timezone.now(),
            content_digest="f" * 64,
        )
    exported = export_user_data(user)
    assert len(exported["bookmarks"]) == len(exported["notes"]) == len(exported["reading"]) == 1
    assert exported["notes"][0]["title"] == user.username
    assert exported["notes"][0]["revision"] == current().revision
    assert exported["reading"][0]["self_reported"] is True
    assert other_user.username not in json.dumps(exported)
    user.delete()
    assert Bookmark.objects.count() == Note.objects.count() == TutorialReading.objects.count() == 1
    assert Note.objects.get().user == other_user
