import uuid

from django.conf import settings
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as gettext


class PublicProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="showcase_profile"
    )
    is_public = models.BooleanField(default=False)
    public_name = models.CharField(max_length=80, blank=True)
    bio = models.TextField(max_length=2000, blank=True, validators=[MaxLengthValidator(2000)])
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_public=False) | ~models.Q(public_name=""),
                name="showcase_public_name_required",
            )
        ]


class SharedResult(models.Model):
    class Metric(models.TextChoices):
        SEQUENTIAL = "sequential-v1", gettext("顺序测试进度")
        GPU_BATCH = "gpu-batch-ms-v1", gettext("GPU 批次计时")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        "core.Submission", on_delete=models.CASCADE, related_name="showcase_entry"
    )
    is_public = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    # Old shares/clients retain their original disclosure scope. A GPU timing
    # requires an explicit new share transition, never an automatic upgrade.
    metric_kind = models.CharField(
        max_length=32,
        choices=Metric.choices,
        default=Metric.SEQUENTIAL,
        db_default=Metric.SEQUENTIAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(metric_kind__in=["sequential-v1", "gpu-batch-ms-v1"]),
                name="showcase_shared_metric_known",
            )
        ]
