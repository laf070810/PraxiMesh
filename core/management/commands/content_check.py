from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.content import ContentError, load_package


class Command(BaseCommand):
    help = "Validate content without executing any reference or user code."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default=str(settings.CONTENT_ROOT / "starter.json"))

    def handle(self, *args, **options):
        try:
            data = load_package(options["path"])
        except (ContentError, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Valid: {len(data['nodes'])} nodes, {len(data['problems'])} problems. References and required DAG checked; code not executed."
        )
