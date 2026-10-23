from django.core.management.base import BaseCommand, CommandError

from challenges.catalog import import_package, load_package
from core.content import ContentError


class Command(BaseCommand):
    help = "Owner-only challenge publication. Omitted activities remain unchanged."

    def add_arguments(self, parser):
        parser.add_argument("package")
        parser.add_argument("--check", action="store_true")

    def handle(self, *args, **options):
        try:
            count = import_package(load_package(options["package"]), check_only=options["check"])
        except (ContentError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"{'Checked' if options['check'] else 'Published'} {count} immutable challenge(s)."
        )
