"""Real HTTP permission/state tests using persisted evaluator-shaped result fixtures.

These tests do not execute untrusted code or claim a real sandbox acceptance run.
"""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, current_thread, main_thread
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from community.models import Post, State
from core.content import digest
from core.models import AccountProfile, Problem, ProblemVersion, Submission
from showcase.models import PublicProfile, SharedResult
from showcase.services import close_profile, export_user_data, lock_profile


def url(name, *args):
    return reverse(f"showcase:{name}", args=args)


def current():
    return Problem.objects.select_related("current").get(pk="sum-positive").current


def joint_environment():
    return {
        "policy": "wasi-functions-v2",
        "policy_digest": "a" * 64,
        "bundle": "b" * 64,
        "python": "3.14.9",
        "quickjs": "2026-06-04",
        "wasmtime": "50.0.1",
        "languages": ["python", "javascript"],
    }


def job(user, *, version=None, passed=None, policy="a", elapsed=10, **overrides):
    version = version or current()
    total = len(version.data["tests"])
    passed = total if passed is None else passed
    verdict = "accepted" if passed == total else "wrong_answer"
    environment = {
        "policy": "wasi-python-function-v1",
        "policy_digest": policy * 64,
        "bundle": "b" * 64,
        "host": "PRIVATE-HOST-SENTINEL",
    }
    fingerprint = hashlib.sha256(f"wasi:{'b' * 64}:{policy * 64}".encode()).hexdigest()
    at = timezone.now()
    values = {
        "user": user,
        "version": version,
        "source": "PRIVATE-SOURCE-SENTINEL",
        "source_digest": "c" * 64,
        "mode": "submit",
        "status": "finished",
        "started_at": at - timedelta(seconds=1),
        "finished_at": at,
        "verdict": verdict,
        "result": {
            "verdict": verdict,
            "label": "UNTRUSTED-LABEL-SENTINEL",
            "passed": passed,
            "total": total,
            "elapsed_ms": elapsed,
            "public_cases": [{"name": "CASE-NAME-SENTINEL"}],
            "unknown": "PRIVATE-RESULT-SENTINEL",
        },
        "environment": environment,
        "evaluation_fingerprint": fingerprint,
        **overrides,
    }
    return Submission.objects.create(**values)


def enable(client, user, **overrides):
    profile = PublicProfile.objects.filter(user=user).first()
    response = client.post(
        url("settings"),
        {
            "revision": profile.revision if profile else 0,
            "public_name": f"Public {user.username}",
            "bio": "Public introduction",
            "is_public": "on",
            "confirm_public": "on",
            **overrides,
        },
    )
    assert response.status_code == 302
    return PublicProfile.objects.get(user=user)


def share(client, item, **overrides):
    profile = PublicProfile.objects.get(user=item.user)
    entry = SharedResult.objects.filter(submission=item).first()
    return client.post(
        url("share", item.pk),
        {
            "profile_revision": profile.revision,
            "entry_revision": entry.revision if entry else 0,
            "confirm_public": "on",
            **overrides,
        },
    )


def shared(client, item):
    response = share(client, item)
    assert response.status_code == 302
    return SharedResult.objects.get(submission=item)


@pytest.mark.parametrize("slug", ["sum-positive", "js-run-length", "sensor-analysis-report"])
def test_joint_runtime_results_use_the_same_explicit_sharing_boundary(
    catalog, logged_client, user, slug
):
    enable(logged_client, user)
    version = Problem.objects.get(pk=slug).current
    item = job(user, version=version, environment=joint_environment())
    entry = shared(logged_client, item)
    response = Client().get(url("entry", entry.pk))
    assert response.status_code == 200
    assert "PRIVATE-SOURCE-SENTINEL" not in response.content.decode()
    assert response.context["entry"]["version_id"] == version.pk


@pytest.mark.parametrize(
    "changes",
    [
        {"quickjs": "unknown"},
        {"languages": ["python"]},
        {"bundle": "e" * 64},
        {"language": "javascript"},
        {"evaluator": "unreviewed-ml"},
        {"policy": "unknown"},
    ],
)
def test_joint_runtime_identity_mismatch_cannot_enter_a_board(
    catalog, logged_client, user, changes
):
    enable(logged_client, user)
    item = job(user, environment={**joint_environment(), **changes})
    assert share(logged_client, item).status_code == 400
    assert not SharedResult.objects.exists()


def next_version(version):
    data = copy.deepcopy(version.data)
    data["revision"] += 1
    data["statement"] = "New reviewed statement"
    result = ProblemVersion.objects.create(
        problem=version.problem, revision=data["revision"], digest=digest(data), data=data
    )
    Problem.objects.filter(pk=version.problem_id).update(current=result)
    return result


def test_default_private_profile_does_not_copy_private_identity(catalog, logged_client, user):
    AccountProfile.objects.create(
        user=user, display_name="PRIVATE-NAME-SENTINEL", recovery_hash="PRIVATE-RECOVERY-SENTINEL"
    )
    item = job(user)
    for endpoint in (url("settings"), url("mine"), url("share", item.pk)):
        response = logged_client.get(endpoint)
        assert response.status_code == 200
        assert response["Cache-Control"] == "no-store"
        assert "PRIVATE-NAME-SENTINEL" not in response.content.decode()
    assert not PublicProfile.objects.exists()
    assert not SharedResult.objects.exists()
    guest = Client()
    assert guest.get(url("profile", user.username)).status_code == 404
    assert not list(guest.get(url("boards")).context["page"])
    assert (
        logged_client.post(
            url("share", item.pk),
            {"profile_revision": 1, "entry_revision": 0, "confirm_public": "on"},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"public_name": " "},
        {"public_name": "x" * 81},
        {"bio": "x" * 2001},
        {"bio": "bad\0bio"},
        {"confirm_public": ""},
        {"revision": "invalid"},
    ],
)
def test_profile_requires_valid_independent_identity_and_consent(logged_client, overrides):
    response = logged_client.post(
        url("settings"),
        {
            "revision": 0,
            "public_name": "Public name",
            "bio": "Introduction",
            "is_public": "on",
            "confirm_public": "on",
            **overrides,
        },
    )
    assert response.status_code == 400
    assert not PublicProfile.objects.exists()


def test_profile_and_result_serialize_only_explicit_public_fields(catalog, logged_client, user):
    enable(logged_client, user, public_name="<script>name</script>", bio="<img src=x>\nplain")
    item = job(user)
    assert share(logged_client, item, confirm_public="").status_code == 400
    assert not SharedResult.objects.exists()
    entry = shared(logged_client, item)
    guest = Client()
    for endpoint in (
        url("profile", user.username),
        url("entry", entry.pk),
        url("board", item.version_id, item.evaluation_fingerprint),
        url("boards"),
    ):
        response = guest.get(endpoint)
        body = response.content.decode()
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert "<script>name</script>" not in body
        assert "<img src=x>" not in body
        for secret in (
            "PRIVATE-SOURCE-SENTINEL",
            "PRIVATE-RESULT-SENTINEL",
            "PRIVATE-HOST-SENTINEL",
            "CASE-NAME-SENTINEL",
            "UNTRUSTED-LABEL-SENTINEL",
            str(item.pk),
        ):
            assert secret not in body
    profile = guest.get(url("profile", user.username))
    assert "&lt;script&gt;name&lt;/script&gt;" in profile.content.decode()
    assert "&lt;img src=x&gt;<br>plain" in profile.content.decode()
    card = guest.get(url("entry", entry.pk)).context["entry"]
    assert card["username"] == user.username and card["label"] == "通过"
    assert not {"submission", "source", "environment", "result", "elapsed_ms"}.intersection(card)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "run"},
        {"status": "queued"},
        {"status": "running"},
        {"verdict": "internal_error"},
        {"verdict": "invented"},
        {"started_at": None},
        {"finished_at": None},
        {"evaluation_fingerprint": ""},
        {"evaluation_fingerprint": "d" * 64},
        {"result": []},
        {"result": "non-object JSON"},
        {"environment": []},
        {"environment": {"policy": "invented"}},
    ],
)
def test_nonformal_unfinished_unverified_results_cannot_be_shared(
    catalog, logged_client, user, overrides
):
    enable(logged_client, user)
    item = job(user, **overrides)
    assert logged_client.get(url("share", item.pk)).status_code == 400
    assert share(logged_client, item).status_code == 400
    assert not SharedResult.objects.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"passed": True},
        {"passed": "1"},
        {"passed": 1.0},
        {"passed": -1},
        {"passed": 1000000},
        {"total": True},
        {"total": "3"},
        {"total": 0},
        {"total": 999},
        {"passed": 0},
        {"verdict": "wrong_answer"},
        {"passed": None},
    ],
)
def test_result_counts_and_verdict_are_strictly_checked(catalog, logged_client, user, mutation):
    enable(logged_client, user)
    item = job(user)
    item.result.update(mutation)
    item.save(update_fields=["result"])
    assert share(logged_client, item).status_code == 400
    assert not SharedResult.objects.exists()


@pytest.mark.parametrize(
    "environment",
    [
        {"policy": "wasi-python-function-v1", "policy_digest": "a" * 64, "bundle": "c" * 64},
        {"policy": "wasi-python-function-v1", "policy_digest": True, "bundle": "b" * 64},
        {"policy": "wasi-python-function-v1", "policy_digest": "a" * 64, "bundle": {}},
        {"policy": "gvisor-python-function-v1", "policy_digest": "a" * 64, "image": "latest"},
        {"policy": "wasi-future", "policy_digest": "a" * 64, "bundle": "b" * 64},
    ],
)
def test_policy_and_artifact_must_match_the_actual_recorded_fingerprint(
    catalog, logged_client, user, environment
):
    enable(logged_client, user)
    assert share(logged_client, job(user, environment=environment)).status_code == 400


def test_gvisor_digest_pinned_results_have_their_own_group(catalog, logged_client, user):
    enable(logged_client, user)
    image = "registry.example/python@sha256:" + "d" * 64
    policy = "e" * 64
    item = job(
        user,
        environment={
            "policy": "gvisor-python-function-v1",
            "policy_digest": policy,
            "image": image,
        },
        evaluation_fingerprint=hashlib.sha256(f"gvisor:{image}:{policy}".encode()).hexdigest(),
    )
    entry = shared(logged_client, item)
    response = Client().get(url("entry", entry.pk))
    assert response.context["entry"]["policy"] == "gvisor-python-function-v1"
    assert image not in response.content.decode()


def test_unsupported_evaluator_and_reversed_timestamps_fail_closed(catalog, logged_client, user):
    enable(logged_client, user)
    item = job(user, finished_at=timezone.now() - timedelta(days=1))
    assert share(logged_client, item).status_code == 400
    item = job(user)
    data = copy.deepcopy(item.version.data)
    data["evaluator"] = "future-project-evaluator"
    ProblemVersion.objects.filter(pk=item.version_id).update(data=data)
    assert share(logged_client, item).status_code == 400


def test_cross_owner_share_withdraw_and_private_list_are_denied(
    catalog, logged_client, user, other_user
):
    enable(logged_client, user)
    own_item = job(user)
    entry = shared(logged_client, own_item)
    other_client = Client()
    other_client.force_login(other_user)
    enable(other_client, other_user)
    assert other_client.get(url("share", own_item.pk)).status_code == 404
    assert share(other_client, own_item).status_code == 404
    assert other_client.post(url("withdraw", entry.pk), {"revision": 1}).status_code == 404
    assert not other_client.get(url("mine")).context["rows"]
    own_profile = PublicProfile.objects.get(user=user)
    response = other_client.post(url("close"), {"revision": 1, "user": user.pk})
    assert response.status_code == 302
    own_profile.refresh_from_db()
    entry.refresh_from_db()
    assert own_profile.is_public and entry.is_public


def test_all_writes_require_login_post_csrf(catalog, logged_client, user):
    enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    endpoints = [url("settings"), url("close"), url("share", item.pk), url("withdraw", entry.pk)]
    guest = Client()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    for endpoint in endpoints:
        assert guest.post(endpoint, {"revision": 1}).status_code == 302
        assert csrf_client.post(endpoint, {"revision": 1}).status_code == 403
    for endpoint in (url("close"), url("withdraw", entry.pk)):
        assert logged_client.get(endpoint).status_code == 405
    assert guest.get(url("mine")).status_code == 302
    assert guest.get(url("settings")).status_code == 302


def test_real_write_rate_limit_is_shared_across_showcase_endpoints(catalog, logged_client, user):
    item = job(user)
    with patch("core.limits.time.time", return_value=1770000000):
        # Invalid writes also consume quota; changing the route cannot reset it.
        for index in range(30):
            endpoint = url("settings") if index % 2 else url("share", item.pk)
            assert logged_client.post(endpoint, {}).status_code == 400
        response = logged_client.post(url("close"), {"revision": 1})
        assert response.status_code == 429 and response["Retry-After"] == "60"
        assert logged_client.get(url("settings")).status_code == 200


def test_board_uses_best_explicit_result_per_user_and_tied_ranks_not_elapsed(
    catalog, logged_client, user, other_user, django_user_model
):
    enable(logged_client, user)
    best = job(user, elapsed=999999)
    shared(logged_client, best)
    shared(logged_client, job(user, passed=0, elapsed=1))
    second = Client()
    second.force_login(other_user)
    enable(second, other_user)
    shared(second, job(other_user, elapsed=1))
    third_user = django_user_model.objects.create_user(username="third")
    third = Client()
    third.force_login(third_user)
    enable(third, third_user)
    third_shared = job(third_user, passed=1)
    shared(third, third_shared)
    job(third_user)  # A better private record never influences a public rank.
    response = Client().get(url("board", best.version_id, best.evaluation_fingerprint))
    rows = list(response.context["page"])
    assert [row["rank"] for row in rows] == [1, 1, 3]
    assert {row["username"] for row in rows[:2]} == {user.username, other_user.username}
    assert rows[2]["passed"] == 1 and rows[2]["username"] == "third"
    assert "999999" not in response.content.decode()
    assert all("elapsed_ms" not in row for row in rows)
    groups = list(Client().get(url("boards")).context["page"])
    assert len(groups) == 1 and groups[0]["participants"] == 3


def test_version_and_policy_groups_never_mix_and_history_is_explicit(catalog, logged_client, user):
    enable(logged_client, user)
    original = job(user)
    first = shared(logged_client, original)
    changed_policy = job(user, policy="d")
    shared(logged_client, changed_policy)
    updated = next_version(original.version)
    recent = job(user, version=updated)
    shared(logged_client, recent)
    guest = Client()
    groups = list(guest.get(url("boards")).context["page"])
    assert len(groups) == 3
    for item in (original, changed_policy, recent):
        response = guest.get(url("board", item.version_id, item.evaluation_fingerprint))
        rows = list(response.context["page"])
        assert len(rows) == 1
        assert rows[0]["version_id"] == item.version_id
        assert rows[0]["fingerprint"] == item.evaluation_fingerprint
        assert rows[0]["historical"] == (item.version_id != updated.pk)
    assert "历史版本" in guest.get(url("entry", first.pk)).content.decode()
    assert (
        guest.get(url("board", updated.pk, changed_policy.evaluation_fingerprint)).status_code
        == 404
    )
    assert guest.get(url("board", 10**30, original.evaluation_fingerprint)).status_code == 404
    assert guest.get(url("board", updated.pk, "invalid")).status_code == 404


def test_withdrawal_and_later_invalid_result_disappear_from_all_public_queries(
    catalog, logged_client, user
):
    enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    guest = Client()
    assert (
        logged_client.post(url("withdraw", entry.pk), {"revision": entry.revision}).status_code
        == 302
    )
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert guest.get(url("board", item.version_id, item.evaluation_fingerprint)).status_code == 404
    assert not list(guest.get(url("profile", user.username)).context["page"])
    assert not list(guest.get(url("boards")).context["page"])
    entry = shared(logged_client, item)
    Submission.objects.filter(pk=item.pk).update(result={"passed": True})
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert not list(guest.get(url("profile", user.username)).context["page"])
    assert not list(guest.get(url("boards")).context["page"])


@pytest.mark.parametrize("disable_via", ["close", "settings"])
def test_closing_profile_revokes_all_results_but_preserves_independent_community_consent(
    catalog, logged_client, user, disable_via
):
    profile = enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    post = Post.objects.create(
        author=user,
        kind="discussion",
        title="Independent writing",
        body="Text",
        state=State.PUBLISHED,
    )
    response = logged_client.post(url(disable_via), {"revision": profile.revision})
    assert response.status_code == 302
    entry.refresh_from_db()
    post.refresh_from_db()
    assert not entry.is_public and post.state == State.PUBLISHED
    guest = Client()
    assert guest.get(url("profile", user.username)).status_code == 404
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert not list(guest.get(url("boards")).context["page"])
    enable(logged_client, user)
    assert guest.get(url("profile", user.username)).status_code == 200
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert not list(guest.get(url("boards")).context["page"])


def test_stale_tabs_cannot_reenable_profile_or_reshare_withdrawn_results(
    catalog, logged_client, user
):
    profile = enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    assert logged_client.post(url("close"), {"revision": profile.revision}).status_code == 302
    assert (
        logged_client.post(
            url("settings"),
            {
                "revision": profile.revision,
                "public_name": "Stale",
                "is_public": "on",
                "confirm_public": "on",
            },
        ).status_code
        == 409
    )
    enable(logged_client, user)
    assert share(logged_client, item, profile_revision=profile.revision).status_code == 409
    assert share(logged_client, item, entry_revision=entry.revision).status_code == 409
    entry = shared(logged_client, item)
    assert (
        logged_client.post(url("withdraw", entry.pk), {"revision": entry.revision - 1}).status_code
        == 409
    )
    assert (
        logged_client.post(url("withdraw", entry.pk), {"revision": entry.revision}).status_code
        == 302
    )
    assert share(logged_client, item, entry_revision=entry.revision).status_code == 409
    assert SharedResult.objects.filter(submission=item).count() == 1
    entry.refresh_from_db()
    assert not entry.is_public


@pytest.mark.parametrize("state", ["unpublished", "no-current", "inactive-user"])
def test_withdrawn_problem_and_inactive_account_remove_public_visibility(
    catalog, logged_client, user, state
):
    enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    if state == "unpublished":
        Problem.objects.filter(pk=item.version.problem_id).update(published=False)
    elif state == "no-current":
        Problem.objects.filter(pk=item.version.problem_id).update(current=None)
    else:
        user.is_active = False
        user.save(update_fields=["is_active"])
    guest = Client()
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert guest.get(url("board", item.version_id, item.evaluation_fingerprint)).status_code == 404
    assert not list(guest.get(url("boards")).context["page"])
    response = guest.get(url("profile", user.username))
    if state == "inactive-user":
        assert response.status_code == 404
    else:
        assert not list(response.context["page"])
        assert share(logged_client, item).status_code == 409


def test_account_delete_and_submission_delete_cascade_all_public_results(
    catalog, logged_client, user
):
    enable(logged_client, user)
    first = job(user)
    first_entry = shared(logged_client, first)
    second_entry = shared(logged_client, job(user))
    first.delete()
    assert not SharedResult.objects.filter(pk=first_entry.pk).exists()
    response = logged_client.post(
        reverse("delete_account"), {"confirm": "DELETE", "password": "Test-passphrase-9621"}
    )
    assert response.status_code == 200
    assert not PublicProfile.objects.exists() and not SharedResult.objects.exists()
    guest = Client()
    assert guest.get(url("entry", second_entry.pk)).status_code == 404
    assert guest.get(url("profile", user.username)).status_code == 404


def test_export_is_owner_scoped_json_and_includes_withdrawn_choices(
    catalog, logged_client, user, other_user
):
    assert export_user_data(other_user) == {"profile": None, "shared_results": []}
    enable(logged_client, user)
    item = job(user)
    entry = shared(logged_client, item)
    logged_client.post(url("withdraw", entry.pk), {"revision": entry.revision})
    other_client = Client()
    other_client.force_login(other_user)
    enable(other_client, other_user, bio="OTHER-PRIVATE-EXPORT-SENTINEL")
    other_entry = shared(other_client, job(other_user))
    data = export_user_data(user)
    serialized = json.dumps(data)
    assert data["profile"]["is_public"]
    assert len(data["shared_results"]) == 1
    assert data["shared_results"][0]["submission_id"] == str(item.pk)
    assert not data["shared_results"][0]["is_public"]
    assert (
        str(other_entry.pk) not in serialized and "OTHER-PRIVATE-EXPORT-SENTINEL" not in serialized
    )
    assert "PRIVATE-SOURCE-SENTINEL" not in serialized and "PRIVATE-HOST-SENTINEL" not in serialized


def test_private_and_public_result_pages_are_reachably_paginated(catalog, logged_client, user):
    enable(logged_client, user)
    entries = [shared(logged_client, job(user)) for _ in range(21)]
    guest = Client()
    for client, endpoint in ((logged_client, url("mine")), (guest, url("profile", user.username))):
        first = client.get(endpoint)
        second = client.get(endpoint, {"page": 2})
        assert len(first.context["page"]) == 20 and len(second.context["page"]) == 1
        assert "?page=2" in first.content.decode() and "?page=1" in second.content.decode()
        assert second.context["page"].number == 2
    second = guest.get(url("profile", user.username), {"page": 2})
    assert second.context["page"][0]["id"] == entries[0].pk


def test_board_pagination_preserves_global_tied_rank(catalog, django_user_model):
    # Batch creation is setup only; earlier tests exercise each consent transition.
    for index in range(21):
        user = django_user_model.objects.create(username=f"person{index:02}")
        PublicProfile.objects.create(user=user, is_public=True, public_name=f"Person {index}")
        item = job(user)
        SharedResult.objects.create(submission=item, is_public=True)
    endpoint = url("board", item.version_id, item.evaluation_fingerprint)
    guest = Client()
    first, second = guest.get(endpoint), guest.get(endpoint, {"page": 2})
    assert len(first.context["page"]) == 20 and len(second.context["page"]) == 1
    assert "?page=2" in first.content.decode()
    assert all(row["rank"] == 1 for row in [*first.context["page"], *second.context["page"]])


def test_group_directory_is_paginated_without_collapsing_versions(catalog, logged_client, user):
    enable(logged_client, user)
    version = current()
    for index in range(21):
        if index:
            version = next_version(version)
        shared(logged_client, job(user, version=version))
    guest = Client()
    first, second = guest.get(url("boards")), guest.get(url("boards"), {"page": 2})
    assert len(first.context["page"]) == 20 and len(second.context["page"]) == 1
    assert "?page=2" in first.content.decode()
    assert second.context["page"][0]["version_id"] == version.pk


@pytest.mark.django_db(transaction=True)
def test_postgresql_inflight_share_observes_profile_close_after_user_lock(catalog, user):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks; SQLite is not concurrency acceptance")
    client = Client()
    client.force_login(user)
    profile = enable(client, user)
    item = job(user)
    ready, start, attempted, acquired = Event(), Event(), Event(), Event()

    def observed_lock(user):
        if current_thread() is main_thread():
            return lock_profile(user)
        attempted.set()
        result = lock_profile(user)
        acquired.set()
        return result

    def stale_request():
        close_old_connections()
        try:
            request_client = Client()
            request_client.force_login(user)
            ready.set()
            assert start.wait(5)
            return request_client.post(
                url("share", item.pk),
                {"profile_revision": profile.revision, "entry_revision": 0, "confirm_public": "on"},
            ).status_code
        finally:
            close_old_connections()

    with (
        patch("showcase.services.lock_profile", side_effect=observed_lock),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(stale_request)
        assert ready.wait(5)
        with transaction.atomic():
            lock_profile(user)
            start.set()
            assert attempted.wait(5)
            assert not acquired.wait(0.2), "The in-flight share must wait for the user row lock"
            close_profile(user, profile.revision)
        assert future.result(timeout=10) == 409
        assert acquired.is_set()
    assert not SharedResult.objects.exists()
