"""Private plans and explicit model requests; no evaluation-result write capability."""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class LearningPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    targets = models.JSONField()
    suggestions = models.JSONField()
    skipped = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"], condition=models.Q(active=True), name="coaching_one_active_plan"
            )
        ]


class HintRequest(models.Model):
    class State(models.TextChoices):
        PREVIEW = "preview", _("等待本人确认发送")
        RUNNING = "running", _("正在生成提示")
        COMPLETE = "complete", _("提示已生成")
        FAILED = "failed", _("辅导暂不可用")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    version = models.ForeignKey("core.ProblemVersion", on_delete=models.PROTECT)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PREVIEW)
    payload = models.JSONField()
    instructions = models.TextField()
    request_digest = models.CharField(max_length=64)
    configuration_digest = models.CharField(max_length=64)
    provider_description = models.CharField(max_length=2000)
    model = models.CharField(max_length=160)
    prompt_version = models.CharField(max_length=40)
    response = models.TextField(blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    truncated = models.BooleanField(default=False)
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    elapsed_ms = models.PositiveIntegerField(null=True)
    charged_day = models.DateField(null=True)
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["state", "lease_until"], name="coaching_active_leases")]


class DailyUsage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    day = models.DateField()
    count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day"], name="coaching_daily_usage")]


class SlotLock(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)


class ExecutionLease(models.Model):
    """Transient capacity reservation; survives deletion without personal data."""

    id = models.UUIDField(primary_key=True, editable=False)
    expires_at = models.DateTimeField(db_index=True)
