"""Immutable source/translation text; publication is an explicit trusted-owner action."""

from django.core.exceptions import ValidationError
from django.db import models


class ImmutableText(models.Model):
    immutable_fields = ()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(*self.immutable_fields).first()
            if previous and any(previous[key] != getattr(self, key) for key in previous):
                raise ValidationError("Published source and translation revisions are immutable.")
        return super().save(*args, **kwargs)


class KnowledgeSnapshot(ImmutableText):
    node = models.ForeignKey(
        "core.KnowledgeNode", on_delete=models.PROTECT, related_name="snapshots"
    )
    source_digest = models.CharField(max_length=64)
    source_data = models.JSONField()
    source_locale = models.CharField(max_length=16, default="zh-hans")
    created_at = models.DateTimeField(auto_now_add=True)
    immutable_fields = ("node_id", "source_digest", "source_data", "source_locale")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["node", "source_digest"], name="unique_knowledge_snapshot"
            )
        ]


class Translation(ImmutableText):
    locale = models.CharField(max_length=16, choices=[("en", "English")])
    revision = models.PositiveIntegerField()
    translation_digest = models.CharField(max_length=64)
    fields = models.JSONField()
    published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class KnowledgeTranslation(Translation):
    snapshot = models.ForeignKey(
        KnowledgeSnapshot, on_delete=models.PROTECT, related_name="translations"
    )
    immutable_fields = ("snapshot_id", "locale", "revision", "translation_digest", "fields")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "locale", "revision"], name="unique_knowledge_translation"
            )
        ]


class ProblemTranslation(Translation):
    version = models.ForeignKey(
        "core.ProblemVersion", on_delete=models.PROTECT, related_name="translations"
    )
    immutable_fields = ("version_id", "locale", "revision", "translation_digest", "fields")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "locale", "revision"], name="unique_problem_translation"
            )
        ]
