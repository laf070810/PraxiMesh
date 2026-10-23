"""Bounded non-executable submissions and independently reviewed package snapshots."""

import copy
import json
import re
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max
from django.http import Http404

from core.content import ContentError, digest, validate_package
from core.models import KnowledgeNode, ProblemVersion
from organizations.models import Membership, Package, PackageRevision
from organizations.services import (
    EDITORS,
    HEX64,
    OrganizationError,
    event,
    require_role,
    slug,
    text,
)

MAX_PACKAGE_BYTES = 75000
REVIEW_PERMISSION = "organizations.review_packages"
CORE_ID = re.compile(r"[a-z][a-z0-9-]{0,79}\Z")


def parse_payload(raw):
    if len(raw) > MAX_PACKAGE_BYTES:
        raise OrganizationError("Package exceeds 75 KB.")

    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError("Duplicate key")
            data[key] = value
        return data

    def invalid_constant(value):
        raise ValueError("Nonfinite value")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise OrganizationError("Expected finite UTF-8 JSON with unique keys.") from None


def package_digest(package, payload):
    return digest(
        {"namespace": package.organization_id, "package": package.slug, "payload": payload}
    )


def prefix(package):
    return f"{package.organization_id}--{package.slug}--"


def validate_payload(package, payload, *, lock=False):
    if not isinstance(payload, dict) or set(payload) != {
        "source_url",
        "license",
        "attribution",
        "content",
        "external",
    }:
        raise OrganizationError(
            "Package requires source_url, license, attribution, content and external."
        )
    if not isinstance(payload["source_url"], str):
        raise OrganizationError("Invalid source URL declaration.")
    try:
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        source = urlsplit(payload["source_url"])
        valid_source = (
            source.scheme == "https"
            and bool(source.hostname)
            and source.username is None
            and source.password is None
            and not source.fragment
            and len(payload["source_url"]) <= 1000
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise OrganizationError("Invalid source declaration or JSON payload.") from None
    if (
        size > MAX_PACKAGE_BYTES
        or not valid_source
        or not text(payload["license"], 160)
        or not text(payload["attribution"], 2000)
    ):
        raise OrganizationError("Invalid source/license declaration or package size.")
    data = payload["content"]
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "nodes", "problems"}
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or not isinstance(data["nodes"], list)
        or not isinstance(data["problems"], list)
        or not data["problems"]
    ):
        raise OrganizationError("Expected a content-v1 package with at least one problem.")
    external = payload["external"]
    if not isinstance(external, list) or len(external) > 100:
        raise OrganizationError(
            "External dependencies must be an explicit allowlist of at most 100 pins."
        )
    owned = copy.deepcopy(data)
    local_ids = {}
    bound_prefix = prefix(package)
    if not slug(package.organization_id, 24) or not slug(package.slug, 24):
        raise OrganizationError("Invalid reserved namespace or package identifier.")
    for item in [*owned["nodes"], *owned["problems"]]:
        if (
            not isinstance(item, dict)
            or not slug(item.get("id"), 80 - len(bound_prefix))
            or item["id"] in local_ids
        ):
            raise OrganizationError(
                "Local IDs must be unique and fit the qualified 80-character identity."
            )
        local_ids[item["id"]] = bound_prefix + item["id"]
    allowed, extra_nodes, extra_problems = {}, [], []
    for pin in external:
        if not isinstance(pin, dict) or pin.get("kind") not in ("node", "problem"):
            raise OrganizationError("Invalid external dependency pin.")
        required = {"kind", "id", "digest"} | ({"revision"} if pin["kind"] == "problem" else set())
        if (
            set(pin) != required
            or not isinstance(pin["id"], str)
            or not CORE_ID.fullmatch(pin["id"])
            or pin["id"] in allowed
            or pin["id"] in local_ids
            or pin["id"].startswith(bound_prefix)
            or not isinstance(pin["digest"], str)
            or not HEX64.fullmatch(pin["digest"])
        ):
            raise OrganizationError(
                "External pins must be unique, immutable and outside this package."
            )
        if pin["kind"] == "node":
            query = KnowledgeNode.objects
            if lock:
                query = query.select_for_update()
            node = query.filter(pk=pin["id"], published=True).first()
            if node is None or digest(node.data) != pin["digest"]:
                raise OrganizationError("An external knowledge pin is unavailable or changed.")
            extra_nodes.append(copy.deepcopy(node.data))
        else:
            if type(pin["revision"]) is not int or not 1 <= pin["revision"] < 2**31:
                raise OrganizationError("Invalid external problem revision.")
            query = ProblemVersion.objects
            if lock:
                query = query.select_for_update(of=("self",))
            version = query.filter(
                problem_id=pin["id"],
                revision=pin["revision"],
                digest=pin["digest"],
                problem__published=True,
                problem__current__isnull=False,
            ).first()
            if version is None or digest(version.data) != version.digest:
                raise OrganizationError("An external problem pin is unavailable or changed.")
            extra_problems.append(copy.deepcopy(version.data))
        allowed[pin["id"]] = pin["id"]

    def reference(value):
        if isinstance(value, str) and value in local_ids:
            return local_ids[value]
        if isinstance(value, str) and value in allowed:
            return value
        raise OrganizationError("A reference is neither local nor explicitly pinned.")

    try:
        for node in owned["nodes"]:
            node["id"] = local_ids[node["id"]]
            for field in ("domains", "topics", "concepts"):
                node[field] = [reference(value) for value in node[field]]
            for edge in node["relations"]:
                edge["target"] = reference(edge["target"])
            for step in node["steps"]:
                step["target"] = reference(step["target"])
        for problem in owned["problems"]:
            problem["id"] = local_ids[problem["id"]]
            problem["concepts"] = [reference(value) for value in problem["concepts"]]
        # External snapshots are validation inputs only. They are never returned,
        # republished, copied to reviewer forms, or claimed as package ownership.
        validate_package(
            {
                "schema_version": 1,
                "nodes": owned["nodes"] + extra_nodes,
                "problems": owned["problems"] + extra_problems,
            }
        )
    except (KeyError, TypeError, ContentError, ValueError, RecursionError):
        raise OrganizationError(
            "Content validation failed; all references and transitive external dependencies must be explicit and valid."
        ) from None
    return owned


def is_reviewer(user):
    return user.is_authenticated and user.has_perm(REVIEW_PERMISSION)


def require_reviewer(user, package):
    if (
        not is_reviewer(user)
        or Membership.objects.filter(organization_id=package.organization_id, user=user).exists()
    ):
        raise PermissionDenied("An independent package reviewer is required.")


@transaction.atomic
def save_package(user, org_id, package_slug, payload):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    if not slug(package_slug, 24):
        raise OrganizationError("Invalid package identifier.")
    package, _ = Package.objects.get_or_create(organization=org, slug=package_slug)
    validate_payload(package, payload)
    number = (package.revisions.aggregate(value=Max("revision"))["value"] or 0) + 1
    if number >= 2**31:
        raise OrganizationError("Package revision limit reached.")
    revision = PackageRevision.objects.create(
        package=package,
        revision=number,
        payload=payload,
        digest=package_digest(package, payload),
        author=user,
    )
    event(org, user, "package_revision", revision.pk, "save")
    return revision


@transaction.atomic
def submit_package(user, org_id, pk, expected_digest):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    try:
        revision = (
            PackageRevision.objects.select_for_update()
            .select_related("package")
            .get(pk=pk, package__organization=org)
        )
    except PackageRevision.DoesNotExist:
        raise Http404 from None
    if (
        revision.state not in (PackageRevision.State.DRAFT, PackageRevision.State.REJECTED)
        or expected_digest != revision.digest
        or package_digest(revision.package, revision.payload) != revision.digest
    ):
        raise OrganizationError("The package revision or state has changed.")
    validate_payload(revision.package, revision.payload)
    revision.state = PackageRevision.State.PENDING
    revision.approved_by, revision.approved_digest, revision.review_note = None, "", ""
    revision.save(update_fields=["state", "approved_by", "approved_digest", "review_note"])
    event(org, user, "package_revision", revision.pk, "submit")


@transaction.atomic
def review_package(user, pk, expected_digest, action, note):
    try:
        initial = PackageRevision.objects.select_related("package").get(pk=pk)
        # Same lock order as organization edits and the trusted publisher.
        from organizations.models import Organization

        org = Organization.objects.select_for_update().get(pk=initial.package.organization_id)
        revision = PackageRevision.objects.select_for_update().select_related("package").get(pk=pk)
    except PackageRevision.DoesNotExist:
        raise Http404 from None
    require_reviewer(user, revision.package)
    if (
        action not in ("approve", "reject")
        or not text(note, 1000)
        or revision.state != PackageRevision.State.PENDING
        or expected_digest != revision.digest
        or package_digest(revision.package, revision.payload) != revision.digest
    ):
        raise OrganizationError(
            "Review requires the exact pending snapshot and a bounded decision note."
        )
    if action == "approve":
        if not Membership.objects.filter(
            organization=org, user_id=revision.author_id, role__in=EDITORS
        ).exists():
            raise OrganizationError("The submitting contributor no longer holds an editing role.")
        validate_payload(revision.package, revision.payload)
        revision.state, revision.approved_by, revision.approved_digest = (
            PackageRevision.State.APPROVED,
            user,
            revision.digest,
        )
    else:
        revision.state, revision.approved_by, revision.approved_digest = (
            PackageRevision.State.REJECTED,
            None,
            "",
        )
    revision.review_note = note
    revision.save(update_fields=["state", "approved_by", "approved_digest", "review_note"])
    event(org, user, "package_revision", revision.pk, action)
    return revision


@transaction.atomic
def request_withdrawal(user, org_id, package_id):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    try:
        package = Package.objects.select_for_update().get(pk=package_id, organization=org)
    except Package.DoesNotExist:
        raise Http404 from None
    if package.state != Package.State.PUBLISHED:
        raise OrganizationError("Only a published package can request withdrawal.")
    package.state = Package.State.WITHDRAWAL_REQUESTED
    package.save(update_fields=["state"])
    event(org, user, "package", package.pk, "request_withdrawal")


@transaction.atomic
def cancel_submission(user, org_id, pk, expected_digest):
    org, _ = require_role(user, org_id, EDITORS, lock=True)
    try:
        revision = PackageRevision.objects.select_for_update().get(pk=pk, package__organization=org)
    except PackageRevision.DoesNotExist:
        raise Http404 from None
    if revision.digest != expected_digest or revision.state in (
        PackageRevision.State.PUBLISHED,
        PackageRevision.State.WITHDRAWN,
    ):
        raise OrganizationError("Only an exact unpublished revision can be cancelled.")
    revision.state = PackageRevision.State.WITHDRAWN
    revision.approved_by, revision.approved_digest = None, ""
    revision.save(update_fields=["state", "approved_by", "approved_digest"])
    event(org, user, "package_revision", revision.pk, "cancel")
