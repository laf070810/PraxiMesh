"""Queue/HTTP budget transitions; PostgreSQL section exercises competing writers."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.db import connection, connections
from django.utils import timezone

from core.models import Problem, Submission, WorkerHealth
from core.queue import (
    AdmissionError,
    begin_execution,
    claim,
    configured_fingerprint,
    enqueue,
    expire_jobs,
    finish,
)
from core.resources import reservation_data, reservation_units, usage
from practice_api.services import submit_once


@pytest.fixture
def ready(catalog, settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_MAX_PENDING = 100
    WorkerHealth.objects.create(
        id="budget-test",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    return Problem.objects.get(pk="sum-positive").current


def test_registry_costs_are_fixed_and_public_selection_is_exact(catalog):
    for slug, multiplier in [
        ("sum-positive", 1),
        ("sensor-analysis-report", 2),
        ("train-linear-model", 1),
        ("weighted-line-fit", 1),
        ("approved-ledger-report", 6),
    ]:
        version = Problem.objects.get(pk=slug).current
        version.data["reservation_units"] = 0
        assert reservation_units(version, "submit") == multiplier * len(version.data["tests"])
        assert reservation_units(version, "run") == multiplier * sum(
            case["public"] for case in version.data["tests"]
        )
        version.data["tests"][0]["public"] = 1
        with pytest.raises(ValueError):
            reservation_units(version, "submit")


def test_exhaustion_is_per_user_and_idempotent_replay_never_charges(
    ready, user, other_user, settings
):
    settings.JUDGE_DAILY_CPU_UNITS = len(ready.data["tests"])
    key = uuid.uuid4()
    original, created = submit_once(user, key, ready.problem_id, ready.pk, "source", "submit")
    assert created and usage(user)["remaining"] == 0
    with pytest.raises(AdmissionError) as error:
        enqueue(user, ready, "again", "run")
    assert error.value.status == 429
    assert enqueue(other_user, ready, "other", "submit").budget_state == "reserved"
    settings.JUDGE_ENABLED = False
    replay, created = submit_once(user, key, ready.problem_id, ready.pk, "source", "submit")
    assert not created and replay.pk == original.pk
    assert Submission.objects.filter(user=user).count() == 1


def test_unstarted_platform_failure_releases_but_first_invocation_consumes(ready, user):
    first = enqueue(user, ready, "source", "submit")
    claimed = claim()
    assert claimed.pk == first.pk and usage(user)["reserved"] == first.budget_units
    assert finish(claimed, {"verdict": "internal_error"}, {}) == 1
    first.refresh_from_db()
    assert first.budget_state == "released" and first.execution_started_at is None
    second = enqueue(user, ready, "syntax error", "submit")
    claimed = claim()
    assert begin_execution(claimed)
    assert begin_execution(claimed)  # Repeated case heartbeat cannot charge twice.
    assert finish(claimed, {"verdict": "syntax_error"}, {}) == 1
    second.refresh_from_db()
    assert second.budget_state == "consumed" and second.execution_started_at is not None
    assert usage(user)["consumed"] == second.budget_units and usage(user)["reserved"] == 0


def test_expiry_retains_started_reservation_and_stale_worker_cannot_release(ready, user):
    pending = enqueue(user, ready, "source", "submit")
    Submission.objects.filter(pk=pending.pk).update(
        created_at=timezone.now() - timedelta(minutes=11)
    )
    expire_jobs()
    pending.refresh_from_db()
    assert pending.budget_state == "released"
    active = enqueue(user, ready, "source", "submit")
    worker = claim()
    assert begin_execution(worker)
    Submission.objects.filter(pk=active.pk).update(
        lease_until=timezone.now() - timedelta(seconds=1)
    )
    assert not begin_execution(worker)
    assert finish(worker, {"verdict": "internal_error"}, {}) == 0
    expire_jobs()
    expire_jobs()
    active.refresh_from_db()
    assert active.budget_state == "consumed" and active.verdict == "internal_error"
    assert usage(user)["consumed"] == active.budget_units


def test_claim_without_execution_is_refunded_on_expiry(ready, user):
    item = enqueue(user, ready, "source", "submit")
    worker = claim()
    with pytest.raises(ValueError, match="execution start"):
        finish(worker, {"verdict": "accepted"}, {})
    Submission.objects.filter(pk=item.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    expire_jobs()
    item.refresh_from_db()
    assert item.budget_state == "released" and item.execution_started_at is None


def test_utc_rollover_keeps_original_reservation_day(ready, user, settings):
    settings.JUDGE_DAILY_CPU_UNITS = len(ready.data["tests"])
    before = datetime(2026, 10, 23, 23, 59, 59, tzinfo=UTC)
    with patch("core.queue.utc_day", return_value=before.date()):
        first = enqueue(user, ready, "source", "submit")
    after = before + timedelta(seconds=2)
    with patch("core.queue.utc_day", return_value=after.date()):
        second = enqueue(user, ready, "source", "submit")
    assert first.budget_day != second.budget_day
    assert usage(user, before.date())["reserved"] == first.budget_units
    assert usage(user, after.date())["reserved"] == second.budget_units


def test_legacy_rows_do_not_invent_history_and_export_delete_are_scoped(
    ready, user, other_user, logged_client
):
    old = Submission.objects.create(
        user=user, version=ready, source="old", source_digest="a" * 64, mode="submit"
    )
    assert reservation_data(old) == {
        "policy": "",
        "day": None,
        "units": 0,
        "state": "legacy",
        "execution_started_at": None,
    }
    own = enqueue(user, ready, "source", "submit")
    foreign = enqueue(other_user, ready, "other", "submit")
    data = logged_client.post("/accounts/export/").json()
    assert data["cpu_budget"]["reserved"] == own.budget_units
    assert {row["id"] for row in data["submissions"]} == {str(old.pk), str(own.pk)}
    assert all("resource_reservation" in row for row in data["submissions"])
    assert (
        logged_client.post(
            "/accounts/delete/", {"password": "Test-passphrase-9621", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    assert list(Submission.objects.values_list("pk", flat=True)) == [foreign.pk]


@pytest.mark.django_db(transaction=True)
def test_competing_admissions_reserve_exactly_one_daily_budget(ready, user, settings):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency matrix")
    settings.JUDGE_DAILY_CPU_UNITS = len(ready.data["tests"])

    def attempt(_):
        try:
            enqueue(user, ready, "source", "submit")
            return True
        except AdmissionError as error:
            assert error.status == 429
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=6) as pool:
        admitted = list(pool.map(attempt, range(6)))
    assert sum(admitted) == 1 and Submission.objects.filter(user=user).count() == 1
    assert usage(user)["remaining"] == 0


@pytest.mark.django_db(transaction=True)
def test_core_migration_preserves_old_jobs_without_backcharging():
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("core", "0002_admission_and_policy")])
    apps = executor.loader.project_state([("core", "0002_admission_and_policy")]).apps
    try:
        owner = apps.get_model("auth", "User").objects.create(username="historical")
        problem = apps.get_model("core", "Problem").objects.create(id="historical")
        version = apps.get_model("core", "ProblemVersion").objects.create(
            problem=problem, revision=1, digest="a" * 64, data={}
        )
        old = apps.get_model("core", "Submission").objects.create(
            user=owner,
            version=version,
            source="private source",
            source_digest="b" * 64,
            mode="submit",
            status="finished",
            verdict="accepted",
            result={"passed": 1},
        )
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0003_cpu_reservations")])
        migrated = Submission.objects.get(pk=old.pk)
        assert migrated.source == "private source" and migrated.result == {"passed": 1}
        assert migrated.budget_state == "legacy" and migrated.budget_day is None
        assert migrated.budget_units == 0 and migrated.execution_started_at is None
    finally:
        MigrationExecutor(connection).migrate([("core", "0003_cpu_reservations")])


@pytest.mark.parametrize("started", [False, True])
def test_worker_exception_refunds_only_before_first_invocation(
    ready, user, settings, monkeypatch, started
):
    from types import SimpleNamespace

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from core.management.commands import judge_worker

    item = enqueue(user, ready, "private", "submit")
    monkeypatch.setattr(
        judge_worker, "configured_sandbox", lambda: SimpleNamespace(preflight=lambda: {})
    )
    monkeypatch.setattr(judge_worker, "check_receipt", lambda environment: None)

    def fail(version, source, mode, sandbox, on_case):
        if started:
            on_case()
        raise ValueError("trusted platform failure")

    monkeypatch.setattr(judge_worker, "evaluate", fail)
    with pytest.raises(CommandError):
        call_command("judge_worker", once=True, worker_id="budget-failure")
    item.refresh_from_db()
    assert item.verdict == "internal_error"
    assert item.budget_state == ("consumed" if started else "released")
    assert bool(item.execution_started_at) is started
