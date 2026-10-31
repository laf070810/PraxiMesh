"""Build a complete review artifact; this command never publishes content."""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from core.content import compose_package, digest


class Command(BaseCommand):
    help = "Append new content IDs to a complete base package without importing or executing code."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--base", required=True)
        parser.add_argument("--append", action="append", required=True, dest="additions")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        try:
            data = compose_package(options["base"], options["additions"])
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n"
            descriptor = os.open(options["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(encoded)
        except (ValueError, OSError, RecursionError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Composed: {len(data['nodes'])} nodes, {len(data['problems'])} problems; "
            f"digest={digest(data)}. Database untouched; code not executed."
        )
