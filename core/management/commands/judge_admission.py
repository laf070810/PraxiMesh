"""Persistent admission pause and bounded, non-destructive queue drain."""

import json
import math
import time
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction

from core.queue import AdmissionError, admission_state, pause_admission, resume_admission


def database_action(action, timeout):
    """Bound server-side SQL waits, then roll back before reporting a failure.

    PostgreSQL timeouts are local to this transaction. SQLite's equivalent only
    bounds lock acquisition; it is restored because it belongs to the connection.
    The caller still needs a process deadline for connection/network failures.
    """
    milliseconds = max(1, math.ceil(timeout * 1000))
    sqlite_timeout = None
    try:
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout")
                sqlite_timeout = cursor.fetchone()[0]
                cursor.execute(f"PRAGMA busy_timeout = {milliseconds}")
        try:
            with transaction.atomic():
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('lock_timeout', %s, true), "
                            "set_config('statement_timeout', %s, true)",
                            [f"{milliseconds}ms", f"{milliseconds}ms"],
                        )
                return action()
        finally:
            if sqlite_timeout is not None:
                with connection.cursor() as cursor:
                    cursor.execute(f"PRAGMA busy_timeout = {sqlite_timeout}")
    except DatabaseError as error:
        raise CommandError(
            "Admission database operation failed or timed out. "
            "No automatic resume or cancellation was attempted; "
            "inspect persisted admission state before retrying."
        ) from error


class Command(BaseCommand):
    help = "Inspect, pause, drain or resume evaluation admission using a deployment UUID."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "pause", "drain", "resume"])
        parser.add_argument("--token", type=uuid.UUID)
        parser.add_argument(
            "--timeout",
            type=float,
            default=180,
            help="Drain waiting budget after pause acquisition; 0 pauses and checks once.",
        )
        parser.add_argument(
            "--database-timeout",
            type=float,
            default=5,
            help="Maximum seconds for each database statement/lock wait (default: 5).",
        )

    def handle(self, *args, **options):
        action, token = options["action"], options["token"]
        if action != "status" and token is None:
            raise CommandError("pause, drain and resume require --token UUID.")
        if token is not None:
            try:
                token = uuid.UUID(str(token))
            except (ValueError, TypeError, AttributeError) as error:
                raise CommandError("--token must be a valid UUID.") from error
        try:
            timeout = float(options["timeout"])
            database_timeout = float(options["database_timeout"])
        except (ValueError, TypeError, OverflowError) as error:
            raise CommandError(
                "--timeout and --database-timeout must be finite numbers."
            ) from error
        if not 0 <= timeout <= 3600:
            raise CommandError("--timeout must be between 0 and 3600 seconds.")
        if not 0 < database_timeout <= 60:
            raise CommandError("--database-timeout must be greater than 0 and at most 60 seconds.")
        try:
            if action in {"pause", "drain"}:
                database_action(lambda: pause_admission(token), database_timeout)
            elif action == "resume":
                database_action(lambda: resume_admission(token), database_timeout)
        except (AdmissionError, ValueError) as error:
            raise CommandError(str(error)) from error
        deadline = time.monotonic() + timeout
        while True:
            state = database_action(admission_state, database_timeout)
            if action == "drain" and state["token"] != str(token):
                raise CommandError("Admission pause changed while draining; no state was reset.")
            if action != "drain" or not (state["queued"] or state["running"]):
                self.stdout.write(json.dumps(state, sort_keys=True))
                return
            if time.monotonic() >= deadline:
                self.stdout.write(json.dumps(state, sort_keys=True))
                raise CommandError(
                    "Drain timed out. Admission remains paused; no jobs were cancelled."
                )
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
