from django.core.management.base import BaseCommand, CommandError

from challenges.catalog import withdraw
from core.content import ContentError


class Command(BaseCommand):
    help = (
        "Withdraw an exact current challenge revision; retain private participation and evidence."
    )

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--expected-digest", required=True)

    def handle(self, *args, **options):
        try:
            withdraw(options["slug"], options["expected_digest"])
        except ContentError as error:
            raise CommandError(str(error)) from error
        self.stdout.write("Challenge withdrawn; private participation and submissions retained.")
