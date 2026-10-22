from datetime import timedelta

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import RateBucket, WorkerHealth
from core.queue import expire_jobs


class Command(BaseCommand):
    help = "Expire abandoned jobs and remove expired rate/session state. Schedule once a minute."

    def handle(self, *args, **options):
        expire_jobs()
        RateBucket.objects.filter(expires_at__lt=timezone.now()).delete()
        Session.objects.filter(expire_date__lt=timezone.now()).delete()
        WorkerHealth.objects.filter(checked_at__lt=timezone.now() - timedelta(minutes=1)).update(
            ready=False
        )
        self.stdout.write("Maintenance complete.")
