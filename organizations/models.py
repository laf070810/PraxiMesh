"""Institution-owned plans and package snapshots; learner records remain private."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    OWNER = "owner", _("组织负责人")
    INSTRUCTOR = "instructor", _("课程教师")
    LEARNER = "learner", _("学习成员")


class Organization(models.Model):
    id = models.SlugField(primary_key=True, max_length=24)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=2000, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Membership(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=12, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="org_one_membership")
        ]
        ordering = ["user_id"]


class Invitation(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", _("等待本人接受")
        ACCEPTED = "accepted", _("已接受")
        REVOKED = "revoked", _("已撤销")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_invitations"
    )
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="organization_invites_sent",
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ImmutableSnapshot(models.Model):
    """State fields may transition; the reviewed or enrolled payload cannot change."""

    immutable_fields = ()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(*self.immutable_fields).first()
            if previous and any(
                previous[field] != getattr(self, field) for field in self.immutable_fields
            ):
                raise ValidationError("Immutable snapshot fields cannot be changed.")
        return super().save(*args, **kwargs)


class Course(ImmutableSnapshot):
    immutable_fields = ("organization_id", "slug")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="courses")
    slug = models.SlugField(max_length=48)
    current = models.ForeignKey(
        "CourseRevision", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published = models.BooleanField(default=False)

    class Meta:
        ordering = ["slug"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="org_course_slug")
        ]


class CourseRevision(ImmutableSnapshot):
    immutable_fields = ("course_id", "revision", "data", "digest", "author_id")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    data = models.JSONField()
    digest = models.CharField(max_length=64)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["course", "revision"], name="org_course_revision")
        ]


class CourseUnit(models.Model):
    revision = models.ForeignKey(CourseRevision, on_delete=models.CASCADE, related_name="units")
    key = models.SlugField(max_length=40)
    position = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=2000)
    problem_version = models.ForeignKey("core.ProblemVersion", on_delete=models.PROTECT)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "key"], name="org_course_unit_key"),
            models.UniqueConstraint(
                fields=["revision", "position"], name="org_course_unit_position"
            ),
        ]


class CourseEnrollment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    revision = models.ForeignKey(CourseRevision, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "revision"], name="org_private_enrollment")
        ]


class Package(ImmutableSnapshot):
    immutable_fields = ("organization_id", "slug")

    class State(models.TextChoices):
        PRIVATE = "private", _("尚未发布")
        PUBLISHED = "published", _("已发布")
        WITHDRAWAL_REQUESTED = "withdrawal_requested", _("等待可信维护者撤回")
        WITHDRAWN = "withdrawn", _("已撤回")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="packages"
    )
    slug = models.SlugField(max_length=24)
    current = models.ForeignKey(
        "PackageRevision", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    state = models.CharField(max_length=24, choices=State.choices, default=State.PRIVATE)

    class Meta:
        ordering = ["slug"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="org_package_slug")
        ]


class PackageRevision(ImmutableSnapshot):
    class State(models.TextChoices):
        DRAFT = "draft", _("私有草稿")
        PENDING = "pending", _("等待审核")
        APPROVED = "approved", _("技术审核通过，等待可信发布")
        REJECTED = "rejected", _("审核未通过")
        PUBLISHED = "published", _("已发布")
        WITHDRAWN = "withdrawn", _("已撤回")

    immutable_fields = ("package_id", "revision", "payload", "digest", "author_id")
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    payload = models.JSONField()
    digest = models.CharField(max_length=64)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="organization_package_contributions",
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.DRAFT)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_package_reviews",
    )
    approved_digest = models.CharField(max_length=64, blank=True)
    review_note = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        permissions = [("review_packages", "Can independently review third-party content packages")]
        constraints = [
            models.UniqueConstraint(fields=["package", "revision"], name="org_package_revision")
        ]


class ManagedObject(models.Model):
    """Identity ownership survives withdrawal and cannot migrate between packages."""

    id = models.SlugField(primary_key=True, max_length=80)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="managed_objects")
    kind = models.CharField(max_length=8, choices=[("node", "node"), ("problem", "problem")])
    digest = models.CharField(max_length=64)
    problem_version = models.ForeignKey(
        "core.ProblemVersion", on_delete=models.PROTECT, null=True, blank=True
    )
    active = models.BooleanField(default=False)


class PublicationLock(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)


class OrganizationEvent(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    target_kind = models.CharField(max_length=20)
    target_id = models.CharField(max_length=80)
    action = models.CharField(max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
