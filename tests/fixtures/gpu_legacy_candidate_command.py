"""Verify fixed CUDA submissions using the configured real worker; no execution fallback."""

import json

from django.core.management.base import BaseCommand

from core.deployment_gpu_check import WAIT_SECONDS, CandidateProbe


class Command(BaseCommand):
    help = "Verify or safely clean original GPU candidate submissions while admission is paused."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["check", "cleanup"])
        for name in (
            "token",
            "round-id",
            "worker-id",
            "cpu-worker-id",
            "device",
            "owner-token",
            "fingerprint",
            "started-at",
        ):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--timeout", type=float, default=WAIT_SECONDS)
        parser.add_argument("--database-timeout", type=float, default=5)

    def handle(self, *args, **options):
        probe = CandidateProbe(
            token=options["token"],
            round_id=options["round_id"],
            worker=options["worker_id"],
            cpu_worker=options["cpu_worker_id"],
            device=options["device"],
            owner=options["owner_token"],
            fingerprint=options["fingerprint"],
            started=options["started_at"],
        )
        result = probe.run(
            cleanup=options["action"] == "cleanup",
            timeout=options["timeout"],
            database_timeout=options["database_timeout"],
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
