from django.core.management.base import BaseCommand, CommandError

from projects.models import Project


class Command(BaseCommand):
    help = "Owner-only withdrawal of a named project; revisions and private evidence are preserved."

    def add_arguments(self, parser):
        parser.add_argument("slug")

    def handle(self, *args, **options):
        if not Project.objects.filter(pk=options["slug"]).update(published=False):
            raise CommandError("Project does not exist.")
        self.stdout.write("Project withdrawn; revisions and submissions were retained.")
