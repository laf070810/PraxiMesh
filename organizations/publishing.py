"""Trusted owner-connection publishing. Never call these mutations from Web views."""

from django.apps import apps
from django.db import connection, transaction
from django.http import Http404

from core.content import digest
from core.models import KnowledgeNode, Problem, ProblemVersion
from localization.packages import snapshot
from organizations.models import (
    ManagedObject,
    Membership,
    Organization,
    Package,
    PackageRevision,
    PublicationLock,
)
from organizations.packages import package_digest, require_reviewer, validate_payload
from organizations.services import EDITORS, OrganizationError, event


def publication_lock():
    if not connection.in_atomic_block:
        raise OrganizationError("Content publication requires an atomic transaction.")
    try:
        return PublicationLock.objects.select_for_update().get(pk=1)
    except PublicationLock.DoesNotExist:
        raise OrganizationError(
            "Organization publication migrations have not initialized the lock."
        ) from None


def official_import_scope(data):
    """Root core importer hook: call inside its atomic transaction before any write.

    Reject collisions before updates, then exclude these returned ID sets from
    official full-package withdrawal. This lock is held until that import ends.
    """
    publication_lock()
    managed = list(ManagedObject.objects.values_list("id", "kind"))
    identities = {key for key, _ in managed}
    reserved = tuple(
        namespace + "--" for namespace in Organization.objects.values_list("pk", flat=True)
    )
    incoming = [item["id"] for group in ("nodes", "problems") for item in data[group]]
    if any(key in identities or (reserved and key.startswith(reserved)) for key in incoming):
        raise OrganizationError("Official import overlaps a reserved third-party package identity.")
    return (
        {key for key, kind in managed if kind == "node"},
        {key for key, kind in managed if kind == "problem"},
    )


def _unowned_collision(identity):
    if (
        KnowledgeNode.objects.filter(pk=identity).exists()
        or Problem.objects.filter(pk=identity).exists()
    ):
        return True
    # Projects and translation bindings share durable identity/version semantics.
    # Never claim an identity solely because its currently visible record vanished.
    checks = (
        ("projects", "Project", {"pk": identity}),
        ("localization", "KnowledgeSnapshot", {"node_id": identity}),
        ("localization", "ProblemTranslation", {"version__problem_id": identity}),
    )
    return any(
        apps.is_installed(app) and apps.get_model(app, model).objects.filter(**query).exists()
        for app, model, query in checks
    )


def _check_owned(managed, package, identity, kind):
    item = managed.get(identity)
    if item is None:
        if _unowned_collision(identity):
            raise OrganizationError(
                "Qualified identity collides with existing content or historical bindings."
            )
        return None
    if item.package_id != package.pk or item.kind != kind:
        raise OrganizationError("Qualified identity belongs to another package or content type.")
    if kind == "node":
        current = KnowledgeNode.objects.filter(pk=identity).first()
        valid = current is not None and digest(current.data) == item.digest
    else:
        current = Problem.objects.select_related("current").filter(pk=identity).first()
        valid = (
            current is not None
            and current.current_id == item.problem_version_id
            and current.current.digest == item.digest
            and digest(current.current.data) == item.digest
        )
    if not valid:
        raise OrganizationError(
            "Owned content has changed outside its package publication history."
        )
    return item


@transaction.atomic
def publish_package(revision_id, expected_digest):
    publication_lock()
    try:
        initial = PackageRevision.objects.select_related("package").get(pk=revision_id)
        org = Organization.objects.select_for_update().get(pk=initial.package.organization_id)
        package = (
            Package.objects.select_for_update(of=("self",))
            .select_related("current")
            .get(pk=initial.package_id)
        )
        revision = (
            PackageRevision.objects.select_for_update(of=("self",))
            .select_related("approved_by")
            .get(pk=revision_id)
        )
    except (PackageRevision.DoesNotExist, Package.DoesNotExist):
        raise Http404 from None
    if package.state == Package.State.WITHDRAWAL_REQUESTED:
        raise OrganizationError("A withdrawal request is pending; publication cannot override it.")
    if (
        revision.state not in (PackageRevision.State.APPROVED, PackageRevision.State.PUBLISHED)
        or not revision.approved_by_id
        or revision.digest != expected_digest
        or revision.approved_digest != revision.digest
        or package_digest(package, revision.payload) != revision.digest
    ):
        raise OrganizationError(
            "Publishing requires the exact independently approved immutable snapshot."
        )
    require_reviewer(revision.approved_by, package)
    if not Membership.objects.filter(
        organization=org, user_id=revision.author_id, role__in=EDITORS
    ).exists():
        raise OrganizationError("The package contributor no longer holds an editing role.")
    already_current = (
        package.current_id == revision.pk
        and package.state == Package.State.PUBLISHED
        and revision.state == PackageRevision.State.PUBLISHED
    )
    if package.current_id and package.current.revision >= revision.revision and not already_current:
        raise OrganizationError("Published package revisions must advance monotonically.")
    owned = validate_payload(package, revision.payload, lock=True)
    identities = {item["id"] for group in ("nodes", "problems") for item in owned[group]}
    managed = {
        item.pk: item
        for item in ManagedObject.objects.select_for_update().filter(pk__in=identities)
    }
    for group, kind in (("nodes", "node"), ("problems", "problem")):
        for item in owned[group]:
            _check_owned(managed, package, item["id"], kind)
    if already_current:
        if (
            set(package.managed_objects.filter(active=True).values_list("pk", flat=True))
            != identities
        ):
            raise OrganizationError("Published ownership does not match the current snapshot.")
        for group, kind in (("nodes", "node"), ("problems", "problem")):
            for item in owned[group]:
                model = KnowledgeNode if kind == "node" else Problem
                if (
                    managed[item["id"]].digest != digest(item)
                    or not model.objects.filter(pk=item["id"], published=True).exists()
                ):
                    raise OrganizationError(
                        "Published content does not match the current snapshot."
                    )
        return revision
    # Missing items are withdrawn only within this one package, never globally.
    absent = list(package.managed_objects.exclude(pk__in=identities))
    KnowledgeNode.objects.filter(pk__in=[item.pk for item in absent if item.kind == "node"]).update(
        published=False
    )
    Problem.objects.filter(pk__in=[item.pk for item in absent if item.kind == "problem"]).update(
        published=False
    )
    package.managed_objects.exclude(pk__in=identities).update(active=False)
    for item in owned["nodes"]:
        previous = KnowledgeNode.objects.filter(pk=item["id"]).first()
        if previous:
            snapshot(previous)
        node, _ = KnowledgeNode.objects.update_or_create(
            id=item["id"],
            defaults={
                "kind": item["kind"],
                "title": item["title"],
                "data": item,
                "published": True,
            },
        )
        snapshot(node)
        ManagedObject.objects.update_or_create(
            id=node.pk,
            defaults={
                "package": package,
                "kind": "node",
                "digest": digest(item),
                "problem_version": None,
                "active": True,
            },
        )
    for item in owned["problems"]:
        problem, _ = Problem.objects.get_or_create(id=item["id"])
        version, created = ProblemVersion.objects.get_or_create(
            problem=problem,
            revision=item["revision"],
            defaults={"data": item, "digest": digest(item)},
        )
        if not created and (version.digest != digest(item) or digest(version.data) != digest(item)):
            raise OrganizationError("An existing immutable problem revision cannot be changed.")
        if problem.current_id and problem.current.revision > version.revision:
            raise OrganizationError("A current problem revision cannot be rolled back.")
        problem.current, problem.published = version, True
        problem.save(update_fields=["current", "published"])
        ManagedObject.objects.update_or_create(
            id=problem.pk,
            defaults={
                "package": package,
                "kind": "problem",
                "digest": version.digest,
                "problem_version": version,
                "active": True,
            },
        )
    package.current, package.state = revision, Package.State.PUBLISHED
    package.save(update_fields=["current", "state"])
    revision.state = PackageRevision.State.PUBLISHED
    revision.save(update_fields=["state"])
    event(org, None, "package_revision", revision.pk, "publish_owner")
    return revision


@transaction.atomic
def withdraw_package(package_id, expected_digest):
    publication_lock()
    try:
        initial = Package.objects.get(pk=package_id)
        org = Organization.objects.select_for_update().get(pk=initial.organization_id)
        package = (
            Package.objects.select_for_update(of=("self",))
            .select_related("current")
            .get(pk=package_id)
        )
    except Package.DoesNotExist:
        raise Http404 from None
    if (
        package.current is None
        or package.current.digest != expected_digest
        or package.state not in (Package.State.PUBLISHED, Package.State.WITHDRAWAL_REQUESTED)
    ):
        raise OrganizationError("Withdrawal requires the currently published package digest.")
    owned = list(package.managed_objects.select_for_update())
    KnowledgeNode.objects.filter(pk__in=[item.pk for item in owned if item.kind == "node"]).update(
        published=False
    )
    Problem.objects.filter(pk__in=[item.pk for item in owned if item.kind == "problem"]).update(
        published=False
    )
    package.managed_objects.update(active=False)
    package.state = Package.State.WITHDRAWN
    package.save(update_fields=["state"])
    package.current.state = PackageRevision.State.WITHDRAWN
    package.current.save(update_fields=["state"])
    event(org, None, "package", package.pk, "withdraw_owner")
