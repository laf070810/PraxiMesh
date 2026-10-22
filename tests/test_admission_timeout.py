import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.utils import timezone

from core.management.commands.judge_admission import database_action
from core.models import ProblemVersion, QueueLock, Submission, WorkerHealth
from core.queue import configured_fingerprint


@pytest.mark.parametrize("value", [0, -1, 61, float("nan"), float("inf"), "invalid", None])
def test_database_timeout_rejects_unbounded_or_invalid_values(db, value):
    with pytest.raises(CommandError, match="database-timeout"):
        call_command("judge_admission", "pause", token=uuid.uuid4(), database_timeout=value)
    assert not QueueLock.objects.exists()


def test_keyword_token_is_normalized_before_drain_and_invalid_token_does_not_write(db):
    token = uuid.uuid4()
    output = io.StringIO()
    call_command("judge_admission", "drain", token=token.hex.upper(), timeout=0, stdout=output)
    assert json.loads(output.getvalue())["token"] == str(token)
    assert QueueLock.objects.get(pk=1).pause_token == token
    with pytest.raises(CommandError, match="valid UUID"):
        call_command("judge_admission", "resume", token="invalid")
    assert QueueLock.objects.get(pk=1).pause_token == token


def test_local_database_timeout_is_restored(db):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite connection-specific busy timeout")
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        original = cursor.fetchone()[0]
    call_command("judge_admission", "status", database_timeout=0.2, stdout=io.StringIO())
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone()[0] == original


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["pause", "drain", "resume", "status"])
def test_postgres_held_queue_lock_fails_within_bound_and_never_commits_later(
    catalog, user, settings, action
):
    if connection.vendor != "postgresql":
        pytest.skip("Production lock timeouts require the PostgreSQL test matrix")
    settings.JUDGE_ENABLED = True
    token = uuid.uuid4()
    original = token if action in {"resume", "status"} else None
    paused_at = timezone.now() if original else None
    QueueLock.objects.create(id=1, pause_token=original, paused_at=paused_at)
    fingerprint = configured_fingerprint()
    WorkerHealth.objects.create(
        id="timeout-test", ready=True, checked_at=timezone.now(), fingerprint=fingerprint
    )
    job = Submission.objects.create(
        user=user,
        version=ProblemVersion.objects.first(),
        source="private source",
        source_digest="a" * 64,
        evaluation_fingerprint=fingerprint,
        mode="submit",
    )
    started = Event()

    def blocked_command():
        try:
            started.set()
            call_command(
                "judge_admission",
                action,
                token=token.hex.upper(),
                timeout=0,
                database_timeout=0.2,
                stdout=io.StringIO(),
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            QueueLock.objects.select_for_update().get(pk=1)
            before = time.monotonic()
            pending = executor.submit(blocked_command)
            assert started.wait(timeout=3)
            with pytest.raises(CommandError, match="database operation failed or timed out"):
                pending.result(timeout=3)
            assert time.monotonic() - before < 3
            # The blocker is still held: failure occurred independently of unlock.
            assert QueueLock.objects.get(pk=1).pause_token == original
        # The command has exited and closed its own connection before unlocking.
        # No deferred pause/resume can commit when the blocking owner exits.
        assert pending.done()
    control = QueueLock.objects.get(pk=1)
    assert control.pause_token == original and control.paused_at == paused_at
    job.refresh_from_db()
    assert job.status == "queued" and job.verdict == ""


@pytest.mark.django_db(transaction=True)
def test_postgres_timeouts_are_transaction_local():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL transaction-local settings")
    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        lock_timeout = cursor.fetchone()[0]
        cursor.execute("SHOW statement_timeout")
        statement_timeout = cursor.fetchone()[0]
    call_command("judge_admission", "status", database_timeout=0.2, stdout=io.StringIO())
    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        assert cursor.fetchone()[0] == lock_timeout
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == statement_timeout


@pytest.mark.django_db(transaction=True)
def test_postgres_statement_timeout_rolls_back_earlier_writes():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL statement cancellation and transaction rollback")

    def write_then_block():
        QueueLock.objects.create(id=1, pause_token=uuid.uuid4(), paused_at=timezone.now())
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(1)")

    with pytest.raises(CommandError, match="database operation failed or timed out"):
        database_action(write_then_block, 0.1)
    assert not QueueLock.objects.exists()
