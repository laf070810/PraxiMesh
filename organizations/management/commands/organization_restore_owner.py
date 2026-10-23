from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Membership, Organization, Role
from organizations.services import event


class Command(BaseCommand):
    help = "Trusted recovery: assign a named owner only to an organization with no remaining owner."

    def add_arguments(self, parser):
        parser.add_argument("organization")
        parser.add_argument("username")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            org = Organization.objects.select_for_update().get(pk=options["organization"])
            user = get_user_model().objects.get(username=options["username"], is_active=True)
        except (Organization.DoesNotExist, get_user_model().DoesNotExist):
            raise CommandError("Organization or active account does not exist.") from None
        if Membership.objects.filter(organization=org, role=Role.OWNER).exists():
            raise CommandError("The organization still has an owner; use its normal role workflow.")
        member, _ = Membership.objects.update_or_create(
            organization=org, user=user, defaults={"role": Role.OWNER}
        )
        event(org, None, "membership", member.pk, "restore_owner")
        self.stdout.write("Owner restored for the previously ownerless organization.")
