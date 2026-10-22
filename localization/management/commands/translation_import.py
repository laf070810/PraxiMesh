from django.core.management.base import BaseCommand, CommandError

from core.content import ContentError
from localization.packages import import_translations, load_translations


class Command(BaseCommand):
    help = "Import a trusted English translation package for exact source versions."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **options):
        try:
            counts = import_translations(load_translations(options["path"]))
        except (ContentError, ValueError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Imported {counts['nodes']} knowledge and {counts['problems']} problem translations."
        )
