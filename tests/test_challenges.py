"""Activity consent, immutable owner content, evidence windows and account isolation."""

import copy
import hashlib
import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from challenges.catalog import import_package, load_package, withdraw
from challenges.models import Challenge, ChallengeRevision, ChallengeTask, Participation
from challenges.progress import export_user_data, state, task_cards
from core.content import ContentError, digest
from core.models import Problem, ProblemVersion, Submission
from learning.achievements import achievements
from learning.services import export_user_data as learning_export

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse(f"challenges:{name}", args=args)


@pytest.fixture
def activity_package(catalog):
    now = timezone.now()
    tasks = []
    for index, slug in enumerate(("population-stats", "range-sums")):
        version = Problem.objects.get(pk=slug).current
        tasks.append(
            {
                "id": f"task-{index}",
                "title": f"Task {index}",
                "description": "Public task summary.",
                "problem": slug,
                "problem_revision": version.revision,
                "problem_digest": version.digest,
            }
        )
    return {
        "schema_version": 1,
        "challenges": [
            {
                "id": "practice-week",
                "revision": 1,
                "title": "Practice week",
                "summary": "Try two tasks.",
                "rules": "Private completion; no public participants or cross-contract score.",
                "starts_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tasks": tasks,
            }
        ],
    }


@pytest.fixture
def published(activity_package):
    import_package(activity_package)
    return Challenge.objects.select_related("current").get(pk="practice-week")


@pytest.fixture
def participation(published, user):
    row = Participation.objects.create(user=user, revision=published.current)
    Participation.objects.filter(pk=row.pk).update(joined_at=timezone.now() - timedelta(hours=1))
    row.refresh_from_db()
    return row


def record(user, version, **changes):
    now = timezone.now()
    created = changes.pop("created_at", now - timedelta(minutes=5))
    total = len(version.data["tests"])
    defaults = {
        "user": user,
        "version": version,
        "source": "PRIVATE-CHALLENGE-SOURCE",
        "source_digest": "a" * 64,
        "mode": "submit",
        "status": "finished",
        "verdict": "accepted",
        "evaluation_fingerprint": hashlib.sha256(
            f"wasi:{'b' * 64}:{'c' * 64}".encode()
        ).hexdigest(),
        "environment": {
            "policy": "wasi-python-function-v1",
            "bundle": "b" * 64,
            "policy_digest": "c" * 64,
        },
        "started_at": now - timedelta(minutes=4),
        "finished_at": now - timedelta(minutes=3),
        "result": {"verdict": "accepted", "passed": total, "total": total},
    }
    row = Submission.objects.create(**(defaults | changes))
    Submission.objects.filter(pk=row.pk).update(created_at=created)
    row.refresh_from_db()
    return row


def test_owner_check_import_pin_protection_and_no_implicit_withdraw(activity_package, tmp_path):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(activity_package))
    call_command("challenge_import", str(path), check=True, stdout=io.StringIO())
    assert not Challenge.objects.exists()
    call_command("challenge_import", str(path), stdout=io.StringIO())
    call_command("challenge_import", str(path), stdout=io.StringIO())
    assert ChallengeRevision.objects.count() == 1 and ChallengeTask.objects.count() == 2
    with pytest.raises(ProtectedError):
        ChallengeTask.objects.first().problem_version.delete()
    original = copy.deepcopy(activity_package)
    activity_package["challenges"][0]["id"] = "another-week"
    import_package(activity_package)
    assert Challenge.objects.get(pk=original["challenges"][0]["id"]).published


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p["challenges"].append(copy.deepcopy(p["challenges"][0])),
        lambda p: p["challenges"][0].update(unknown=True),
        lambda p: p["challenges"][0].update(id="mine"),
        lambda p: p["challenges"][0].update(revision=True),
        lambda p: p["challenges"][0].update(revision=2**31),
        lambda p: p["challenges"][0].update(title=" "),
        lambda p: p["challenges"][0].update(rules="x" * 4001),
        lambda p: p["challenges"][0].update(starts_at="2026-02-30T00:00:00Z"),
        lambda p: p["challenges"][0].update(starts_at="2026-01-01T00:00:00+08:00"),
        lambda p: p["challenges"][0].update(ends_at=p["challenges"][0]["starts_at"]),
        lambda p: p["challenges"][0].update(tasks=[]),
        lambda p: p["challenges"][0]["tasks"][1].update(problem="population-stats"),
        lambda p: p["challenges"][0]["tasks"][1].update(id="task-0"),
        lambda p: p["challenges"][0]["tasks"][0].update(problem_digest="0" * 64),
        lambda p: p["challenges"][0]["tasks"][0].update(problem_revision=False),
    ],
)
def test_malformed_or_unresolvable_content_rolls_back(activity_package, change):
    change(activity_package)
    with pytest.raises(ContentError):
        import_package(activity_package)
    assert not Challenge.objects.exists() and not ChallengeTask.objects.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":NaN}',
        b"\xff",
        b"x" * 1_000_001,
        b"[" * 10000,
    ],
)
def test_bounded_strict_utf8_json(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(ContentError):
        load_package(path)


def test_changed_revision_and_bad_second_plan_roll_back(published, activity_package):
    activity_package["challenges"][0]["title"] = "Changed in place"
    with pytest.raises(ContentError):
        import_package(activity_package)
    activity_package["challenges"][0]["revision"] = 2
    invalid = copy.deepcopy(activity_package["challenges"][0])
    invalid["id"] = "second"
    invalid["tasks"][0]["problem_digest"] = "f" * 64
    activity_package["challenges"].append(invalid)
    with pytest.raises(ContentError):
        import_package(activity_package)
    assert ChallengeRevision.objects.count() == 1
    published.refresh_from_db()
    assert published.current.revision == 1


def test_consent_idempotence_revision_pins_and_leave_rejoin(
    published, activity_package, logged_client, user
):
    endpoint = url("participate", published.pk, 1)
    assert logged_client.get(endpoint).status_code == 405
    assert not Participation.objects.exists()
    assert logged_client.post(endpoint, {"action": "join", "user": 999}).status_code == 302
    row = Participation.objects.get(user=user)
    logged_client.post(endpoint, {"action": "join"})
    assert Participation.objects.get().joined_at == row.joined_at
    activity_package["challenges"][0]["revision"] = 2
    import_package(activity_package)
    assert Participation.objects.get().revision_id == published.current_id
    assert logged_client.post(endpoint, {"action": "join"}).status_code == 409
    assert logged_client.get(url("revision", published.pk, 1)).context["participation"].pk == row.pk
    logged_client.post(endpoint, {"action": "leave"})
    assert not Participation.objects.exists()
    endpoint = url("participate", published.pk, 2)
    logged_client.post(endpoint, {"action": "join"})
    assert Participation.objects.get().joined_at > row.joined_at


def test_post_csrf_and_owner_only_join_identity(published, user, other_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    endpoint = url("participate", published.pk, 1)
    assert client.post(endpoint, {"action": "join"}).status_code == 403
    client.get(url("detail", published.pk))
    assert (
        client.post(
            endpoint,
            {
                "action": "join",
                "user": other_user.pk,
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            },
        ).status_code
        == 302
    )
    assert Participation.objects.get().user_id == user.pk


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "run"},
        {"status": "running"},
        {"verdict": "internal_error"},
        {"result": {}},
        {"evaluation_fingerprint": "0" * 64},
        {
            "budget_state": "reserved",
            "budget_policy": "cpu-invocations-v1",
            "budget_day": timezone.now().date(),
            "budget_units": 1,
        },
        {"started_at": None},
    ],
)
def test_nonformal_or_untrustworthy_results_do_not_count(participation, user, change):
    revision = participation.revision
    record(user, revision.tasks.first().problem_version, **change)
    assert all(card["evidence"] is None for card in task_cards(revision, participation))


@pytest.mark.parametrize(
    "case",
    [
        "before_join",
        "before_start",
        "at_end",
        "completed_at_end",
        "started_before_created",
        "future_finish",
    ],
)
def test_window_and_time_ordering_exclusions(participation, user, case):
    revision = participation.revision
    now = timezone.now()
    changes = {
        "before_join": {"created_at": participation.joined_at - timedelta(microseconds=1)},
        "before_start": {"created_at": revision.starts_at - timedelta(microseconds=1)},
        "at_end": {
            "created_at": revision.ends_at,
            "started_at": revision.ends_at,
            "finished_at": revision.ends_at,
        },
        "completed_at_end": {"finished_at": revision.ends_at},
        "started_before_created": {
            "created_at": now,
            "started_at": now - timedelta(seconds=2),
            "finished_at": now - timedelta(seconds=1),
        },
        "future_finish": {"finished_at": now + timedelta(minutes=2)},
    }[case]
    record(user, revision.tasks.first().problem_version, **changes)
    assert task_cards(revision, participation)[0]["evidence"] is None


def test_inclusive_start_own_evidence_and_ended_read_only_progress(participation, user, other_user):
    revision = participation.revision
    Participation.objects.filter(pk=participation.pk).update(
        joined_at=revision.starts_at - timedelta(hours=1)
    )
    participation.refresh_from_db()
    first, second = list(revision.tasks.select_related("problem_version"))
    own = record(
        user,
        first.problem_version,
        created_at=revision.starts_at,
        started_at=revision.starts_at,
        finished_at=revision.starts_at + timedelta(seconds=1),
    )
    record(other_user, second.problem_version)
    cards = task_cards(revision, participation, now=revision.ends_at + timedelta(days=1))
    assert cards[0]["evidence"]["id"] == own.pk and cards[1]["evidence"] is None
    assert state(revision, revision.starts_at) == "active"
    assert state(revision, revision.ends_at) == "ended"
    assert state(revision, revision.starts_at - timedelta(microseconds=1)) == "upcoming"


def test_other_problem_revision_and_withdrawn_tasks_do_not_count(
    participation, user, logged_client
):
    revision = participation.revision
    original = revision.tasks.first().problem_version
    data = copy.deepcopy(original.data)
    data["revision"] += 1
    newer = ProblemVersion.objects.create(
        problem=original.problem, revision=data["revision"], digest=digest(data), data=data
    )
    Problem.objects.filter(pk=original.problem_id).update(current=newer)
    record(user, newer)
    assert task_cards(revision, participation)[0]["evidence"] is None
    response = logged_client.get(url("task", revision.challenge_id, revision.revision, "task-0"))
    assert response.context["task"]["archived"]
    assert (
        "reference" not in response.context["problem"]
        and "tests" not in response.context["problem"]
    )
    own = record(user, original)
    assert task_cards(revision, participation)[0]["evidence"]["id"] == own.pk
    Problem.objects.filter(pk=original.problem_id).update(published=False)
    assert not task_cards(revision, participation)[0]["available"]
    assert task_cards(revision, participation)[0]["evidence"] is None
    assert (
        logged_client.get(
            url("task", revision.challenge_id, revision.revision, "task-0")
        ).status_code
        == 404
    )


def test_withdrawal_export_leave_and_delete_keep_only_own_records(
    participation, user, other_user, logged_client
):
    revision = participation.revision
    Participation.objects.create(user=other_user, revision=revision)
    with pytest.raises(CommandError):
        call_command("challenge_withdraw", revision.challenge_id, expected_digest="0" * 64)
    withdraw(revision.challenge_id, revision.digest)
    assert logged_client.get(url("detail", revision.challenge_id)).status_code == 404
    assert revision.data["title"] not in logged_client.get(url("mine")).content.decode()
    exported = export_user_data(user)
    assert len(exported) == 1 and exported[0]["revision_digest"] == revision.digest
    assert "PRIVATE" not in json.dumps(exported)
    assert learning_export(user)["challenges"] == exported
    user.delete()
    assert (
        Participation.objects.count() == 1 and Participation.objects.get().user_id == other_user.pk
    )
    assert Challenge.objects.count() == ChallengeRevision.objects.count() == 1


def test_cross_account_progress_and_private_achievements(
    participation, user, other_user, logged_client, client
):
    revision = participation.revision
    for task in revision.tasks.select_related("problem_version"):
        record(user, task.problem_version)
        record(user, task.problem_version)
    own = {row["id"]: row for row in achievements(user)}
    assert own["first-pass-v1"]["achieved"] and own["five-problems-v1"]["count"] == 2
    assert own["three-contracts-v1"]["count"] == 1
    assert own["challenge-complete-v1"]["achieved"]
    assert not any(row["achieved"] for row in achievements(other_user))
    response = logged_client.get(url("detail", revision.challenge_id))
    assert response.context["completed"] == 2
    client.force_login(other_user)
    other = client.get(url("detail", revision.challenge_id), {"user": user.pk})
    assert other.context["participation"] is None and other.context["completed"] == 0
    assert "PRIVATE-CHALLENGE-SOURCE" not in response.content.decode()
    assert not any(
        row["achieved"]
        for row in client.get("/learning/achievements/", {"user": user.pk}).context["achievements"]
    )
    client.logout()
    assert client.get("/learning/achievements/").status_code == 302
    assert client.get(url("mine")).status_code == 302


def test_english_controls_and_discovery(published, user):
    client = Client(HTTP_ACCEPT_LANGUAGE="en")
    client.force_login(user)
    assert "Join this activity" in client.get(url("detail", published.pk)).content.decode()
    assert "Project plans" not in client.get(url("detail", published.pk)).content.decode()
    assert "My achievements" in client.get("/learning/achievements/").content.decode()
    body = client.get("/practice/", {"evaluator": "python-ml-linear-v1"}).content.decode()
    assert "ML training" in body and "Maximum reservation units per formal submission" in body


def test_leaving_and_rejoining_same_revision_does_not_restore_previous_progress(
    participation, logged_client, user
):
    revision = participation.revision
    record(user, revision.tasks.first().problem_version)
    assert task_cards(revision, participation)[0]["evidence"]
    endpoint = url("participate", revision.challenge_id, revision.revision)
    logged_client.post(endpoint, {"action": "leave"})
    logged_client.post(endpoint, {"action": "join"})
    new = Participation.objects.get(user=user)
    assert new.joined_at > participation.joined_at
    assert task_cards(revision, new)[0]["evidence"] is None


def test_upcoming_can_join_but_ended_cannot(published, logged_client):
    revision = published.current
    now = timezone.now()
    ChallengeRevision.objects.filter(pk=revision.pk).update(starts_at=now + timedelta(hours=1))
    endpoint = url("participate", published.pk, 1)
    assert logged_client.post(endpoint, {"action": "join"}).status_code == 302
    logged_client.post(endpoint, {"action": "leave"})
    ChallengeRevision.objects.filter(pk=revision.pk).update(
        starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1)
    )
    assert logged_client.post(endpoint, {"action": "join"}).status_code == 409
    assert not Participation.objects.exists()


def test_completed_project_achievement_requires_all_exact_pins(activity_package, user):
    from projects.catalog import import_package as import_project

    tasks = activity_package["challenges"][0]["tasks"]
    stages = [
        {**task, "final": index == 1, "recommended": [tasks[0]["id"]] if index == 1 else []}
        for index, task in enumerate(tasks)
    ]
    import_project(
        {
            "schema_version": 1,
            "projects": [
                {
                    "id": "test-project",
                    "revision": 1,
                    "title": "Test",
                    "summary": "Evidence test",
                    "integration_goal": "Combine both components",
                    "stages": stages,
                }
            ],
        }
    )
    first = Problem.objects.get(pk=tasks[0]["problem"])
    second = Problem.objects.get(pk=tasks[1]["problem"])
    record(user, first.current)
    assert (
        next(row for row in achievements(user) if row["id"] == "project-complete-v1")["count"] == 0
    )
    record(user, second.current)
    assert (
        next(row for row in achievements(user) if row["id"] == "project-complete-v1")["count"] == 1
    )
    second.published = False
    second.save(update_fields=["published"])
    assert (
        next(row for row in achievements(user) if row["id"] == "project-complete-v1")["count"] == 0
    )


def test_three_distinct_contracts_and_public_sample(catalog, user, settings):
    import_package(load_package(settings.CONTENT_ROOT / "challenges.json"))
    for slug in ("sum-positive", "js-run-length", "sensor-analysis-report"):
        record(
            user,
            Problem.objects.get(pk=slug).current,
            environment={
                "policy": "wasi-functions-v2",
                "bundle": "b" * 64,
                "policy_digest": "c" * 64,
                "languages": ["python", "javascript"],
                "python": "3.14.9",
                "quickjs": "2026-06-04",
                "wasmtime": "50.0.1",
            },
        )
    result = {row["id"]: row for row in achievements(user)}
    assert result["three-contracts-v1"]["achieved"]
    assert result["five-problems-v1"]["count"] == 3
