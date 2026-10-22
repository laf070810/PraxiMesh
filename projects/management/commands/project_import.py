from django.core.management.base import BaseCommand, CommandError

from core.content import ContentError
from projects.catalog import import_package, load_package


class Command(BaseCommand):
    help = "Owner-only project publication. Import only named plans; other projects stay unchanged."

    def add_arguments(self, parser):
        parser.add_argument("package")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Validate pins and immutability without publishing.",
        )

    def handle(self, *args, **options):
        try:
            count = import_package(load_package(options["package"]), check_only=options["check"])
        except (ContentError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"{'Checked' if options['check'] else 'Published'} {count} immutable project plan(s)."
        )
