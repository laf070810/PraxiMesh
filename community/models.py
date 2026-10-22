"""Moderated public writing; private submissions are never imported here."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class State(models.TextChoices):
    DRAFT = "draft", "私有草稿"
    PENDING = "pending", "等待审核"
    PUBLISHED = "published", "已公开"
    REJECTED = "rejected", "审核未通过"
    HIDDEN = "hidden", "已隐藏"
    WITHDRAWN = "withdrawn", "已撤回"


class Writing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField(max_length=20000)
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.DRAFT, db_index=True
    )
    revision = models.PositiveIntegerField(default=1)
    moderation_note = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at", "-id"]


class Post(Writing):
    class Kind(models.TextChoices):
        DISCUSSION = "discussion", "讨论"
        QUESTION = "question", "提问"
        SOLUTION = "solution", "题解"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    title = models.CharField(max_length=160)
    version = models.ForeignKey(
        "core.ProblemVersion", on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta(Writing.Meta):
        permissions = [("moderate_content", "Can review community content, reports and appeals")]
        constraints = [
            models.CheckConstraint(
                condition=~Q(kind="solution") | Q(version__isnull=False),
                name="community_solution_has_version",
            )
        ]


class Comment(Writing):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=5000)


class Report(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        RESOLVED = "resolved", "已处理"
        DISMISSED = "dismissed", "未采纳"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, null=True, blank=True)
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, null=True, blank=True)
    reason = models.CharField(max_length=2000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    resolution = models.CharField(max_length=1000, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="community_report_decisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(post__isnull=False, comment__isnull=True)
                | Q(post__isnull=True, comment__isnull=False),
                name="community_report_one_target",
            )
        ]


class Appeal(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        REOPENED = "reopened", "已重新送审"
        UPHELD = "upheld", "维持原决定"
        SUPERSEDED = "superseded", "内容已修订或撤回"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, null=True, blank=True)
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, null=True, blank=True)
    target_revision = models.PositiveIntegerField()
    reason = models.CharField(max_length=2000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    resolution = models.CharField(max_length=1000, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="community_appeal_decisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(post__isnull=False, comment__isnull=True)
                | Q(post__isnull=True, comment__isnull=False),
                name="community_appeal_one_target",
            )
        ]


class ModerationEvent(models.Model):
    """Retained after deletion: no writing, reasons, author IDs or usernames."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    target_kind = models.CharField(max_length=12)
    target_id = models.UUIDField()
    target_revision = models.PositiveIntegerField(default=0)
    action = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
