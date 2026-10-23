from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from organizations.publishing import withdraw_package
from organizations.services import OrganizationError


class Command(BaseCommand):
    help = "Owner-only: withdraw one package's current content without deleting history."

    def add_arguments(self, parser):
        parser.add_argument("package")
        parser.add_argument("--digest", required=True)

    def handle(self, *args, **options):
        try:
            withdraw_package(options["package"], options["digest"])
        except (OrganizationError, Http404, ValueError, ValidationError) as error:
            raise CommandError("Package withdrawal rejected: " + str(error)) from error
        self.stdout.write("Only the selected package's content was withdrawn; history retained.")
