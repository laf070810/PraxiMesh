from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.content import ContentError, import_package, load_package


class Command(BaseCommand):
    help = "Atomically publish a complete reviewed content package. Missing IDs are withdrawn."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default=str(settings.CONTENT_ROOT / "starter.json"))
        parser.add_argument(
            "--translations", help="Trusted translation package, imported in the same transaction"
        )

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                import_package(load_package(options["path"]))
                if options["translations"]:
                    from localization.packages import import_translations, load_translations

                    import_translations(load_translations(options["translations"]))
        except (ContentError, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("Content imported atomically; existing revisions preserved.")
