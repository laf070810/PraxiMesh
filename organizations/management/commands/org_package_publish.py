from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from organizations.publishing import publish_package
from organizations.services import OrganizationError


class Command(BaseCommand):
    help = "Owner-only: atomically publish an independently approved exact package revision."

    def add_arguments(self, parser):
        parser.add_argument("revision", type=int)
        parser.add_argument("--digest", required=True)

    def handle(self, *args, **options):
        try:
            publish_package(options["revision"], options["digest"])
        except (OrganizationError, PermissionDenied, Http404) as error:
            raise CommandError("Package publication rejected: " + str(error)) from error
        self.stdout.write("Approved package published atomically within its ownership scope.")
