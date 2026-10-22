from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models


class Bookmark(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    problem = models.ForeignKey("core.Problem", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["user", "problem"], name="unique_learning_bookmark")
        ]


class Note(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=120)
    body = models.TextField(max_length=10000, validators=[MaxLengthValidator(10000)])
    version = models.ForeignKey(
        "core.ProblemVersion", null=True, blank=True, on_delete=models.PROTECT
    )
    node = models.ForeignKey("core.KnowledgeNode", null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(version__isnull=True) | models.Q(node__isnull=True),
                name="learning_note_one_target",
            )
        ]

    def clean(self):
        super().clean()
        if self.version_id and self.node_id:
            raise ValidationError("一份笔记只能关联一个题目版本或知识节点。")


class TutorialReading(models.Model):
    """A user's own reading declaration, tied to the content seen at that time."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tutorial = models.ForeignKey("core.KnowledgeNode", on_delete=models.CASCADE)
    completed_at = models.DateTimeField()
    content_digest = models.CharField(max_length=64)

    class Meta:
        ordering = ["-completed_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["user", "tutorial"], name="unique_tutorial_reading")
        ]
