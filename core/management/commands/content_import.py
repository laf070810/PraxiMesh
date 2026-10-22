from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.content import ContentError, import_package, load_package


class Command(BaseCommand):
    help = "Atomically publish a complete reviewed content package. Missing IDs are withdrawn."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default=str(settings.CONTENT_ROOT / "starter.json"))

    def handle(self, *args, **options):
        try:
            import_package(load_package(options["path"]))
        except (ContentError, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("Content imported atomically; existing revisions preserved.")
