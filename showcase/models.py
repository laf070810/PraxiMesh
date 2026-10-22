import uuid

from django.conf import settings
from django.core.validators import MaxLengthValidator
from django.db import models


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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        "core.Submission", on_delete=models.CASCADE, related_name="showcase_entry"
    )
    is_public = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
