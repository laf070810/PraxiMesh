import logging
import socket
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.management.commands.sandbox_check import check_receipt
from core.models import WorkerHealth
from core.queue import begin_execution, claim, configured_fingerprint, finish
from judge.backends import configured_sandbox
from judge.evaluator import evaluate
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

logger = logging.getLogger(__name__)


class LeaseLost(Exception):
    """Discard deleted/expired tasks without disabling other users' worker."""


class Command(BaseCommand):
    help = "Run the explicitly configured isolated backend with pinned artifacts and a fresh sandbox_check receipt."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--worker-id", default=socket.gethostname())

    def handle(self, *args, **options):
        worker_id = options["worker_id"]
        # Never advertise a newly edited on-disk policy as this process's code.
        # Deployment must restart workers with immutable, reviewed artifacts.
        startup_fingerprint = configured_fingerprint()

        def health(ready):
            WorkerHealth.objects.update_or_create(
                id=worker_id,
                defaults={
                    "ready": ready,
                    "checked_at": timezone.now(),
                    "fingerprint": startup_fingerprint,
                },
            )

        health(False)
        if not settings.JUDGE_ENABLED:
            raise CommandError("JUDGE_ENABLED is off. No submissions will be executed.")
        try:
            sandbox = configured_sandbox()
            environment = sandbox.preflight()
            check_receipt(environment)
            while True:
                # Receipts expire even during long-lived workers; admission then stops.
                check_receipt(environment)
                health(True)
                job = claim(startup_fingerprint)
                if job:

                    def heartbeat(current_job=job):
                        if not begin_execution(current_job):
                            raise LeaseLost
                        health(True)

                    try:
                        result = evaluate(
                            job.version.data, job.source, job.mode, sandbox, heartbeat
                        )
                    except LeaseLost:
                        logger.info("judge_result_discarded job=%s reason=lease_lost", job.id)
                    except Exception:
                        # No source, input, raw stderr or environment secrets in logs.
                        logger.error("judge_internal_error job=%s", job.id)
                        finish(
                            job,
                            {"verdict": "internal_error", "label": LABELS["internal_error"]},
                            environment,
                        )
                        raise
                    else:
                        if finish(job, result, environment):
                            logger.info(
                                "judge_finished job=%s verdict=%s", job.id, result["verdict"]
                            )
                        else:
                            logger.info("judge_result_discarded job=%s reason=lease_lost", job.id)
                if options["once"]:
                    return
                time.sleep(1)
        except (SandboxUnavailable, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            health(False)
