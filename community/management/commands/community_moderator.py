from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Grant or revoke only the explicit community moderation permission."

    def add_arguments(self, parser):
        parser.add_argument("username")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--grant", action="store_true")
        action.add_argument("--revoke", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError("No account with that username.")
        permission = Permission.objects.filter(
            content_type__app_label="community", codename="moderate_content"
        ).first()
        if permission is None:
            raise CommandError("Run migrations before configuring community moderation.")
        if options["grant"]:
            user.user_permissions.add(permission)
        else:
            user.user_permissions.remove(permission)
        self.stdout.write(
            "Community moderation permission granted."
            if options["grant"]
            else "Direct community moderation permission revoked; group and superuser permissions are unchanged."
        )
