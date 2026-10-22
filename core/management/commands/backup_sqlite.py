import os
import sqlite3
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Consistent online SQLite backup; refuses to overwrite an existing destination."

    def add_arguments(self, parser):
        parser.add_argument("destination")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if database["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("Use pg_dump and the PostgreSQL recovery runbook for production.")
        source = Path(database["NAME"])
        target = Path(options["destination"]).absolute()
        if not source.is_file() or target.exists() or target.is_symlink():
            raise CommandError("Source must exist; destination must be a new file.")
        if not target.parent.is_dir():
            raise CommandError("Create a private backup directory first.")
        fd, temp_name = tempfile.mkstemp(prefix=".praximesh-backup-", dir=target.parent)
        os.close(fd)
        try:
            with sqlite3.connect(source) as origin, sqlite3.connect(temp_name) as destination:
                origin.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CommandError("Backup integrity check failed.")
            os.link(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        self.stdout.write(f"Verified SQLite backup written: {target}")
