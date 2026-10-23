from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError

from organizations.packages import REVIEW_PERMISSION


class Command(BaseCommand):
    help = "Grant or revoke only organizations.review_packages for a named account."

    def add_arguments(self, parser):
        parser.add_argument("username")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--grant", action="store_true")
        group.add_argument("--revoke", action="store_true")

    def handle(self, *args, **options):
        try:
            user = get_user_model().objects.get(username=options["username"])
            permission = Permission.objects.get(
                content_type__app_label="organizations", codename=REVIEW_PERMISSION.split(".")[1]
            )
        except (get_user_model().DoesNotExist, Permission.DoesNotExist):
            raise CommandError(
                "Account or organization review permission does not exist."
            ) from None
        (user.user_permissions.add if options["grant"] else user.user_permissions.remove)(
            permission
        )
        self.stdout.write("Only the explicit organization package review permission was updated.")
