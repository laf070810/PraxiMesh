"""Fresh, real safety probes before each packaged WASI worker starts."""

import socket

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify the packaged WASI runtime on this host, then start its worker."

    def add_arguments(self, parser):
        parser.add_argument("--worker-id", default=socket.gethostname())
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.JUDGE_ENABLED or settings.JUDGE_BACKEND != "wasi":
            raise CommandError("The WASI service requires JUDGE_ENABLED=1 and JUDGE_BACKEND=wasi.")
        call_command("sandbox_check")
        call_command("judge_worker", worker_id=options["worker_id"], once=options["once"])
