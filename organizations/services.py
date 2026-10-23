"""Organization-scoped authorization; all mutations serialize on their organization."""

import json
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import Http404
from django.utils import timezone

from core.content import digest
from core.models import KnowledgeNode, Problem, ProblemVersion
from organizations.models import (
    Course,
    CourseEnrollment,
    CourseRevision,
    CourseUnit,
    Invitation,
    Membership,
    Organization,
    OrganizationEvent,
    Role,
)

EDITORS = (Role.OWNER, Role.INSTRUCTOR)
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class OrganizationError(ValueError):
    pass


def text(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and "\0" not in value
    )


def slug(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and SLUG.fullmatch(value)


def event(org, actor, kind, target, action):
    OrganizationEvent.objects.create(
        organization=org, actor=actor, target_kind=kind, target_id=str(target), action=action
    )


def require_role(user, org_id, roles=None, *, lock=False):
    if not user.is_authenticated:
        raise PermissionDenied
    query = Organization.objects.select_for_update() if lock else Organization.objects
    try:
        org = query.get(pk=org_id)
        member = Membership.objects.get(organization=org, user=user)
    except (Organization.DoesNotExist, Membership.DoesNotExist):
        raise Http404 from None
    if roles is not None and member.role not in roles:
        raise PermissionDenied
    return org, member


@transaction.atomic
def create_organization(user, namespace, name, description):
    if not user.is_authenticated:
        raise PermissionDenied
    if (
        not slug(namespace, 24)
        or not text(name, 120)
        or not isinstance(description, str)
        or len(description) > 2000
        or "\0" in description
    ):
        raise OrganizationError("Invalid organization identity or text.")
    # Identical names are not identities; a reserved namespace cannot be claimed again.
    if (
        KnowledgeNode.objects.filter(pk__startswith=namespace + "--").exists()
        or Problem.objects.filter(pk__startswith=namespace + "--").exists()
    ):
        raise OrganizationError("The namespace overlaps existing content identities.")
    if namespace in {"new", "packages", "review", "invitations"}:
        raise OrganizationError("This namespace is reserved for application routes.")
    if Organization.objects.filter(pk=namespace).exists():
        raise OrganizationError("This namespace is already reserved.")
    try:
        with transaction.atomic():
            org = Organization.objects.create(
                id=namespace, name=name.strip(), description=description, created_by=user
            )
    except IntegrityError:
        raise OrganizationError("This namespace is already reserved.") from None
    Membership.objects.create(organization=org, user=user, role=Role.OWNER)
    event(org, user, "organization", org.pk, "create")
    return org


@transaction.atomic
def invite(user, org_id, username, role):
    org, _ = require_role(user, org_id, (Role.OWNER,), lock=True)
    if role not in Role.values:
        raise OrganizationError("Invalid organization role.")
    try:
        target = get_user_model().objects.get(username=username, is_active=True)
    except get_user_model().DoesNotExist:
        raise OrganizationError("The target account cannot receive this invitation.") from None
    if Membership.objects.filter(organization=org, user=target).exists():
        raise OrganizationError("The account is already a member.")
    if Invitation.objects.filter(
        organization=org, target=target, state=Invitation.State.PENDING
    ).exists():
        raise OrganizationError("A pending invitation already exists.")
    item = Invitation.objects.create(organization=org, target=target, inviter=user, role=role)
    event(org, user, "invitation", item.pk, "invite")
    return item


@transaction.atomic
def invitation_action(user, pk, action):
    try:
        initial = Invitation.objects.get(pk=pk)
        org = Organization.objects.select_for_update().get(pk=initial.organization_id)
        item = Invitation.objects.select_for_update().get(pk=pk)
    except (Invitation.DoesNotExist, Organization.DoesNotExist):
        raise Http404 from None
    if not user.is_authenticated or (
        item.target_id != user.pk
        and not Membership.objects.filter(organization=org, user=user, role=Role.OWNER).exists()
    ):
        raise Http404
    if item.state != Invitation.State.PENDING:
        raise OrganizationError("The invitation is no longer pending.")
    if action == "accept" and item.target_id == user.pk:
        if not Membership.objects.filter(
            organization=org, user_id=item.inviter_id, role=Role.OWNER
        ).exists():
            raise OrganizationError(
                "The inviting owner is no longer authorized; request a new invitation."
            )
        if Membership.objects.filter(organization=org, user=user).exists():
            raise OrganizationError("A membership already exists.")
        Membership.objects.create(organization=org, user=user, role=item.role)
        item.state = Invitation.State.ACCEPTED
    elif action == "revoke":
        item.state = Invitation.State.REVOKED
    else:
        raise Http404
    item.save(update_fields=["state"])
    event(org, user, "invitation", item.pk, action)


@transaction.atomic
def change_member(user, org_id, member_id, role=None):
    org, actor = require_role(user, org_id, lock=True)
    try:
        member = Membership.objects.get(pk=member_id, organization=org)
    except Membership.DoesNotExist:
        raise Http404 from None
    if actor.role != Role.OWNER and not (role is None and member.user_id == user.pk):
        raise PermissionDenied
    if role is not None and role not in Role.values:
        raise OrganizationError("Invalid organization role.")
    if (
        member.role == Role.OWNER
        and role != Role.OWNER
        and Membership.objects.filter(organization=org, role=Role.OWNER).count() == 1
    ):
        raise OrganizationError(
            "Assign another owner before the last owner leaves or changes role."
        )
    if role is None:
        target = member.pk
        member.delete()
        event(org, user, "membership", target, "remove")
    else:
        member.role = role
        member.save(update_fields=["role"])
        event(org, user, "membership", member.pk, "role")


def validate_course(data):
    if (
        not isinstance(data, dict)
        or set(data) != {"title", "summary", "units"}
        or not text(data["title"], 160)
        or not text(data["summary"], 2000)
        or not isinstance(data["units"], list)
        or not 1 <= len(data["units"]) <= 30
    ):
        raise OrganizationError("Expected a title, summary and 1–30 pinned course units.")
    try:
        size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise OrganizationError("Invalid course JSON text.") from None
    if size > 20000:
        raise OrganizationError("Course plan exceeds 20 KB.")
    keys, versions = set(), []
    for item in data["units"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "title", "description", "problem", "revision", "digest"}
            or not slug(item["key"], 40)
            or item["key"] in keys
            or not text(item["title"], 160)
            or not text(item["description"], 2000)
            or not isinstance(item["problem"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", item["problem"])
            or type(item["revision"]) is not int
            or not 1 <= item["revision"] < 2**31
            or not isinstance(item["digest"], str)
            or not HEX64.fullmatch(item["digest"])
        ):
            raise OrganizationError("Invalid course unit or immutable problem binding.")
        version = (
            ProblemVersion.objects.select_related("problem")
            .filter(
                problem_id=item["problem"],
                revision=item["revision"],
                digest=item["digest"],
                problem__published=True,
                problem__current__isnull=False,
            )
            .first()
        )
        if version is None:
            raise OrganizationError(
                "A course unit refers to an unavailable or mismatched problem version."
            )
        versions.append(version)
        keys.add(item["key"])
    return versions


@transaction.atomic
def save_course(user, org_id, course_slug, data):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    if not slug(course_slug, 48):
        raise OrganizationError("Invalid course identifier.")
    versions = validate_course(data)
    course, _ = Course.objects.get_or_create(organization=org, slug=course_slug)
    number = (course.revisions.aggregate(value=Max("revision"))["value"] or 0) + 1
    if number >= 2**31:
        raise OrganizationError("Course revision limit reached.")
    revision = CourseRevision.objects.create(
        course=course, revision=number, data=data, digest=digest(data), author=user
    )
    for position, (item, version) in enumerate(zip(data["units"], versions, strict=True)):
        CourseUnit.objects.create(
            revision=revision,
            key=item["key"],
            position=position,
            title=item["title"],
            description=item["description"],
            problem_version=version,
        )
    event(org, user, "course_revision", revision.pk, "save")
    return revision


@transaction.atomic
def course_action(user, org_id, pk, expected_digest, action):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    try:
        revision = CourseRevision.objects.select_related("course__current").get(
            pk=pk, course__organization=org
        )
    except CourseRevision.DoesNotExist:
        raise Http404 from None
    course = revision.course
    if revision.digest != expected_digest or digest(revision.data) != revision.digest:
        raise OrganizationError("Course snapshot does not match the requested digest.")
    if action == "publish":
        verify_course_snapshot(revision)
        validate_course(revision.data)
        if course.current_id and course.current.revision > revision.revision:
            raise OrganizationError("A newer course revision has already been published.")
        revision.published_at = revision.published_at or timezone.now()
        revision.save(update_fields=["published_at"])
        course.current, course.published = revision, True
    elif action == "withdraw":
        course.published = False
    else:
        raise Http404
    course.save(update_fields=["current", "published"])
    event(org, user, "course_revision", revision.pk, action)


@transaction.atomic
def enroll(user, org_id, pk, action):
    if not user.is_authenticated:
        raise PermissionDenied
    if action == "leave":
        records = CourseEnrollment.objects.filter(
            user=user, revision_id=pk, revision__course__organization_id=org_id
        )
        if not records.exists():
            raise Http404
        records.delete()
        return
    org, _ = require_role(user, org_id, lock=True)
    try:
        revision = CourseRevision.objects.get(
            pk=pk, course__organization=org, course__published=True, published_at__isnull=False
        )
    except CourseRevision.DoesNotExist:
        raise Http404 from None
    if action == "join":
        CourseEnrollment.objects.get_or_create(user=user, revision=revision)
    elif action == "leave":
        CourseEnrollment.objects.filter(user=user, revision=revision).delete()
    else:
        raise Http404


def verify_course_snapshot(revision):
    if digest(revision.data) != revision.digest:
        raise OrganizationError("Course snapshot digest changed.")
    units = list(revision.units.select_related("problem_version"))
    items = revision.data.get("units")
    if not isinstance(items, list) or len(units) != len(items):
        raise OrganizationError("Course units do not match the immutable plan.")
    for position, (unit, item) in enumerate(zip(units, items, strict=True)):
        version = unit.problem_version
        if (
            unit.position != position
            or unit.key != item["key"]
            or unit.title != item["title"]
            or unit.description != item["description"]
            or version.problem_id != item["problem"]
            or version.revision != item["revision"]
            or version.digest != item["digest"]
            or digest(version.data) != version.digest
        ):
            raise OrganizationError("A stored course unit no longer matches its immutable binding.")
