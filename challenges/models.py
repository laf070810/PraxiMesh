"""Owner-published activities and private, explicitly requested participation."""

from django.conf import settings
from django.db import models


class Challenge(models.Model):
    id = models.SlugField(primary_key=True, max_length=80)
    published = models.BooleanField(default=False)
    current = models.ForeignKey(
        "ChallengeRevision", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        ordering = ["id"]


class ChallengeRevision(models.Model):
    challenge = models.ForeignKey(Challenge, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    data = models.JSONField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["challenge", "revision"], name="challenges_revision"),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="challenges_window"
            ),
        ]


class ChallengeTask(models.Model):
    revision = models.ForeignKey(ChallengeRevision, on_delete=models.CASCADE, related_name="tasks")
    key = models.SlugField(max_length=80)
    position = models.PositiveSmallIntegerField()
    problem_version = models.ForeignKey("core.ProblemVersion", on_delete=models.PROTECT)
    data = models.JSONField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "key"], name="challenges_task_key"),
            models.UniqueConstraint(
                fields=["revision", "position"], name="challenges_task_position"
            ),
            models.UniqueConstraint(
                fields=["revision", "problem_version"], name="challenges_task_pin"
            ),
        ]


class Participation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    revision = models.ForeignKey(ChallengeRevision, on_delete=models.PROTECT)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-joined_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "revision"], name="challenges_unique_participant"
            ),
        ]
