"""Persistent admission pause and bounded, non-destructive queue drain."""

import json
import time
import uuid

from django.core.management.base import BaseCommand, CommandError

from core.queue import AdmissionError, admission_state, pause_admission, resume_admission


class Command(BaseCommand):
    help = "Inspect, pause, drain or resume evaluation admission using a deployment UUID."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "pause", "drain", "resume"])
        parser.add_argument("--token", type=uuid.UUID)
        parser.add_argument("--timeout", type=float, default=180)

    def handle(self, *args, **options):
        action, token = options["action"], options["token"]
        if action != "status" and token is None:
            raise CommandError("pause, drain and resume require --token UUID.")
        if not 0 <= options["timeout"] <= 3600:
            raise CommandError("--timeout must be between 0 and 3600 seconds.")
        try:
            if action in {"pause", "drain"}:
                pause_admission(token)
            elif action == "resume":
                resume_admission(token)
        except (AdmissionError, ValueError) as error:
            raise CommandError(str(error)) from error
        deadline = time.monotonic() + options["timeout"]
        while True:
            state = admission_state()
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
