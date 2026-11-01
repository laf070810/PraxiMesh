"""Container deployment entry; full startup is mandatory before every claim loop."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from core.container_worker_database import check_database
from core.gpu_worker import StopFlag, run_worker, stop_signals
from judge.container_adapter import ContainerGPUAdapter
from judge.container_controller import STATE, ContainerController
from judge.container_worker_config import read_config
from judge.container_worker_image import verify_image


class Command(BaseCommand):
    help = "Run the trusted container GPU worker using its private worker.json configuration."
    requires_system_checks = []

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--once", action="store_true", help="Full startup, then at most one claim."
        )
        group.add_argument(
            "--check",
            action="store_true",
            help="Read-only image/configuration/database checks only; never access GPU or publish ready.",
        )

    def handle(self, *args, **options):
        stage = "image"
        try:
            verify_image()
            stage = "configuration"
            config = read_config()
            if getattr(settings, "WORKER_CONFIGURATION", None) != config:
                raise ValueError(
                    "The fixed container worker settings and private configuration are required"
                )
            if not options["check"] and not settings.JUDGE_ENABLED:
                stage = "disabled"
                raise ValueError("Container GPU work is disabled in worker.json")
            stage = "database"
            check_database()
            if options["check"]:
                self.stdout.write(
                    "Worker image and PostgreSQL checks passed; GPU readiness was not checked."
                )
                return
            stop = StopFlag()
            stage = "controller"
            with stop_signals(stop), ContainerController(config["gpu"]) as control:
                if control.saved["version"] != 4:
                    raise ValueError(
                        "Container GPU workers require the full v4 observation contract"
                    )
                adapter = ContainerGPUAdapter(
                    control, image=config["task_image"], driver_manifest=config["driver_manifest"]
                )
                stage = "worker"
                run_worker(
                    control.guard,
                    config["worker_id"],
                    adapter,
                    receipt_directory=STATE / "receipts",
                    stop=stop,
                    once=options["once"],
                    container_journal=control.journal,
                )
        except Exception as error:
            raise CommandError(
                f"Container GPU worker stopped: stage={stage} error={type(error).__name__}"
            ) from None
        finally:
            connections.close_all()
