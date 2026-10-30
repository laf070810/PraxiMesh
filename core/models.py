import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class KnowledgeNode(models.Model):
    """Reviewed, validated graph records. Stable IDs survive title changes."""

    id = models.SlugField(primary_key=True, max_length=80)
    kind = models.CharField(max_length=16, db_index=True)
    title = models.CharField(max_length=200)
    data = models.JSONField()
    published = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]


class Problem(models.Model):
    id = models.SlugField(primary_key=True, max_length=80)
    current = models.ForeignKey(
        "ProblemVersion", null=True, on_delete=models.PROTECT, related_name="+"
    )
    published = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]


class ProblemVersion(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.PROTECT, related_name="versions")
    revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["problem", "revision"], name="unique_problem_revision")
        ]


class AccountProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=80, blank=True)
    recovery_hash = models.CharField(max_length=256)


class Draft(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    version = models.ForeignKey(ProblemVersion, on_delete=models.CASCADE)
    source = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "version"], name="unique_user_draft")
        ]


class Enrollment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    path = models.ForeignKey(KnowledgeNode, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "path"], name="unique_enrollment")]


class Submission(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", _("排队中")
        RUNNING = "running", _("正在评测")
        FINISHED = "finished", _("评测结束")

    class Mode(models.TextChoices):
        RUN = "run", _("公开样例")
        SUBMIT = "submit", _("正式提交")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    version = models.ForeignKey(ProblemVersion, on_delete=models.PROTECT)
    source = models.TextField()
    source_digest = models.CharField(max_length=64)
    evaluation_fingerprint = models.CharField(max_length=64, blank=True, default="")
    mode = models.CharField(max_length=10, choices=Mode.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    verdict = models.CharField(max_length=24, blank=True)
    result = models.JSONField(default=dict)
    environment = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    # Legacy rows retain no invented resource accounting or execution timestamp.
    budget_policy = models.CharField(max_length=32, blank=True, default="")
    budget_day = models.DateField(null=True)
    budget_units = models.PositiveIntegerField(default=0)
    budget_state = models.CharField(
        max_length=12,
        default="legacy",
        choices=[
            ("legacy", "Legacy"),
            ("reserved", "Reserved"),
            ("consumed", "Consumed"),
            ("released", "Released"),
        ],
    )
    execution_started_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(
                fields=["user", "budget_day", "budget_state"], name="submission_cpu_budget"
            ),
            models.Index(
                fields=["status", "evaluation_fingerprint", "created_at"],
                name="submission_policy_queue",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        budget_state="legacy",
                        budget_policy="",
                        budget_day__isnull=True,
                        budget_units=0,
                        execution_started_at__isnull=True,
                    )
                    | (
                        models.Q(
                            budget_policy__in=["cpu-invocations-v1", "gpu-slot-seconds-v1"],
                            budget_day__isnull=False,
                            budget_units__gte=1,
                        )
                        & (
                            models.Q(
                                budget_state__in=["reserved", "released"],
                                execution_started_at__isnull=True,
                            )
                            | models.Q(budget_state="consumed", execution_started_at__isnull=False)
                        )
                    )
                ),
                name="submission_resource_reservation",
            )
        ]


class WorkerHealth(models.Model):
    id = models.CharField(primary_key=True, max_length=80)
    checked_at = models.DateTimeField()
    ready = models.BooleanField(default=False)
    fingerprint = models.CharField(max_length=64)


class GPUDevice(models.Model):
    """Operational reservation survives deletion/expiry until physical recovery."""

    class State(models.TextChoices):
        QUARANTINED = "quarantined", "Quarantined"
        READY = "ready", "Ready"
        RESERVED = "reserved", "Reserved"

    id = models.CharField(primary_key=True, max_length=80)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUARANTINED)
    worker_id = models.CharField(max_length=80)
    owner_token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    # Published only after the complete startup suite; no private receipt or source.
    environment = models.JSONField(default=dict)
    checked_at = models.DateTimeField(null=True)
    # No FK: account deletion must not release physical device ownership.
    job_id = models.UUIDField(null=True, unique=True)
    reservation_token = models.UUIDField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(job_id__isnull=True, reservation_token__isnull=True)
                    | models.Q(job_id__isnull=False, reservation_token__isnull=False)
                ),
                name="gpu_reservation_identity_pair",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="quarantined")
                    | models.Q(state="ready", job_id__isnull=True, checked_at__isnull=False)
                    | models.Q(state="reserved", job_id__isnull=False, checked_at__isnull=False)
                ),
                name="gpu_device_state_shape",
            ),
        ]


class RateBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)


class QueueLock(models.Model):
    id = models.PositiveIntegerField(primary_key=True, default=1)
    pause_token = models.UUIDField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
