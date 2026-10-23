"""Real database/HTTP permissions and scoped publishing; no sandbox verdicts are invented."""

import copy
import io
import json

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse

from core.content import digest
from core.models import KnowledgeNode, Problem, ProblemVersion
from localization.models import KnowledgeSnapshot
from organizations import packages, publishing, services
from organizations.export import export_user_data
from organizations.models import (
    Course,
    CourseEnrollment,
    CourseRevision,
    Invitation,
    ManagedObject,
    Membership,
    Organization,
    OrganizationEvent,
    Package,
    PackageRevision,
    PublicationLock,
    Role,
)
from projects.models import Project

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse("organizations:" + name, args=args)


@pytest.fixture
def org(user):
    PublicationLock.objects.get_or_create(pk=1)
    return services.create_organization(
        user, "sample-org", "Private institution", "Private organization text"
    )


@pytest.fixture
def reviewer(django_user_model):
    user = django_user_model.objects.create_user(
        username="org-reviewer", password="Review-Passphrase-1199"
    )
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="organizations", codename="review_packages")
    )
    return user


@pytest.fixture
def third_user(django_user_model):
    return django_user_model.objects.create_user(
        username="unrelated-user", password="Other-Passphrase-1199"
    )


@pytest.fixture
def payload(catalog):
    domain = copy.deepcopy(next(item for item in catalog["nodes"] if item["kind"] == "domain"))
    domain.update(
        id="domain",
        title="Synthetic local domain",
        domains=[],
        topics=[],
        concepts=[],
        relations=[],
        steps=[],
    )
    concept = copy.deepcopy(next(item for item in catalog["nodes"] if item["kind"] == "concept"))
    concept.update(
        id="concept",
        title="Synthetic local concept",
        domains=["domain"],
        topics=[],
        concepts=[],
        relations=[],
        aliases=[],
        steps=[],
    )
    problem = copy.deepcopy(
        next(item for item in catalog["problems"] if item["id"] == "sum-positive")
    )
    problem.update(id="positive-sum", revision=1, concepts=["concept"])
    return {
        "source_url": "https://example.org/declared-source",
        "license": "LicenseRef-Pending-Maintainer-Approval",
        "attribution": "Synthetic organization fixture; no public licensing claim.",
        "content": {"schema_version": 1, "nodes": [domain, concept], "problems": [problem]},
        "external": [],
    }


def add_member(owner, org, user, role):
    invite = services.invite(owner, org.pk, user.username, role)
    services.invitation_action(user, invite.pk, "accept")
    return Membership.objects.get(organization=org, user=user)


def approved(owner, org, reviewer, payload, slug="basics"):
    revision = packages.save_package(owner, org.pk, slug, payload)
    packages.submit_package(owner, org.pk, revision.pk, revision.digest)
    packages.review_package(
        reviewer,
        revision.pk,
        revision.digest,
        "approve",
        "Reviewed exact fixture; deployment rights remain pending.",
    )
    revision.refresh_from_db()
    assert revision.state == PackageRevision.State.APPROVED
    return revision


def publish(revision):
    call_command(
        "org_package_publish", str(revision.pk), digest=revision.digest, stdout=io.StringIO()
    )
    revision.refresh_from_db()
    return revision


def course_plan():
    version = Problem.objects.get(pk="sum-positive").current
    return {
        "title": "Private course",
        "summary": "Pinned units, private evidence",
        "units": [
            {
                "key": "first",
                "title": "Private unit",
                "description": "Local teaching text",
                "problem": version.problem_id,
                "revision": version.revision,
                "digest": version.digest,
            }
        ],
    }


@pytest.mark.parametrize(
    "slug", ["train-linear-model", "weighted-line-fit", "approved-ledger-report"]
)
def test_course_counts_valid_specialist_evidence_only_for_its_learner(
    catalog, org, user, other_user, slug
):
    from test_agent_integration import job_fixture as agent_job
    from test_specialist_integration import job_fixture as specialist_job

    from organizations.progress import own_progress

    version = Problem.objects.get(pk=slug).current
    plan = course_plan()
    plan["units"][0].update(problem=slug, revision=version.revision, digest=version.digest)
    course = services.save_course(user, org.pk, "specialists", plan)
    services.course_action(user, org.pk, course.pk, course.digest, "publish")
    job = (agent_job if slug == "approved-ledger-report" else specialist_job)(user, version)
    assert own_progress(course, user)[0]["evidence"]["id"] == job.pk
    assert own_progress(course, other_user)[0]["evidence"] is None
    job.result = {}
    job.save(update_fields=["result"])
    assert own_progress(course, user)[0]["evidence"] is None


def test_invitation_requires_target_acceptance_and_current_owner_role(
    org, user, other_user, third_user, client
):
    client.force_login(user)
    assert (
        client.post(
            url("invite", org.pk), {"username": other_user.username, "role": "instructor"}
        ).status_code
        == 302
    )
    invitation = Invitation.objects.get()
    assert not Membership.objects.filter(organization=org, user=other_user).exists()
    client.force_login(third_user)
    assert client.post(url("invitation_action", invitation.pk, "accept")).status_code == 404
    client.force_login(other_user)
    assert client.get(url("detail", org.pk)).status_code == 404
    assert client.post(url("invitation_action", invitation.pk, "accept")).status_code == 302
    assert Membership.objects.get(organization=org, user=other_user).role == Role.INSTRUCTOR
    assert (
        client.post(
            url("invite", org.pk), {"username": third_user.username, "role": "owner"}
        ).status_code
        == 403
    )
    second = services.invite(user, org.pk, third_user.username, Role.LEARNER)
    owner = Membership.objects.get(organization=org, user=user)
    with pytest.raises(services.OrganizationError):
        services.change_member(user, org.pk, owner.pk, Role.LEARNER)
    services.change_member(
        user, org.pk, Membership.objects.get(organization=org, user=other_user).pk, Role.OWNER
    )
    services.change_member(other_user, org.pk, owner.pk, Role.LEARNER)
    with pytest.raises(services.OrganizationError):
        services.invitation_action(third_user, second.pk, "accept")
    services.invitation_action(other_user, second.pk, "revoke")


def test_member_roster_and_courses_are_not_visible_outside_role_scope(
    org, user, other_user, third_user, client
):
    add_member(user, org, other_user, Role.LEARNER)
    client.force_login(other_user)
    response = client.get(url("detail", org.pk))
    assert response.status_code == 200 and response.context["members"] is None
    assert client.get(url("course_create", org.pk)).status_code == 403
    assert client.get(url("package_create", org.pk)).status_code == 403
    client.force_login(third_user)
    assert client.get(url("detail", org.pk)).status_code == 404
    assert (
        client.post(
            url("invite", org.pk), {"username": third_user.username, "role": "owner"}
        ).status_code
        == 404
    )
    client.logout()
    assert client.get(url("detail", org.pk)).status_code == 302


def test_course_pins_private_evidence_and_revision_history(
    catalog, org, user, other_user, third_user, client
):
    from test_showcase import job

    add_member(user, org, other_user, Role.LEARNER)
    plan = course_plan()
    client.force_login(user)
    response = client.post(
        url("course_create", org.pk),
        {"slug": "first-course", "plan": json.dumps(plan), "acknowledge": "on"},
    )
    assert response.status_code == 302
    revision = CourseRevision.objects.get()
    course = revision.course
    client.force_login(other_user)
    assert client.get(url("course", org.pk, course.pk)).status_code == 404
    assert (
        client.post(
            url("course_action", org.pk, revision.pk, "publish"), {"digest": revision.digest}
        ).status_code
        == 403
    )
    services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    assert client.post(url("enrollment", org.pk, revision.pk, "join")).status_code == 302
    learner_job = job(other_user)
    job(third_user)
    response = client.get(url("course", org.pk, course.pk))
    assert response.context["completed"] == 1
    assert str(learner_job.pk) in response.content.decode()
    assert "PRIVATE-SOURCE-SENTINEL" not in response.content.decode()
    client.force_login(user)
    response = client.get(url("course", org.pk, course.pk), {"user": other_user.pk})
    assert not response.context["joined"] and response.context["completed"] == 0
    assert str(learner_job.pk) not in response.content.decode()
    second = services.save_course(
        user, org.pk, course.slug, {**plan, "title": "New draft revision"}
    )
    client.force_login(other_user)
    assert client.get(url("course_revision", org.pk, course.pk, second.revision)).status_code == 404
    version = Problem.objects.get(pk="sum-positive").current
    newer_data = copy.deepcopy(version.data)
    newer_data.update(revision=version.revision + 1, title="New problem version")
    newer = ProblemVersion.objects.create(
        problem=version.problem,
        revision=newer_data["revision"],
        data=newer_data,
        digest=digest(newer_data),
    )
    Problem.objects.filter(pk=version.problem_id).update(current=newer)
    response = client.get(url("course", org.pk, course.pk))
    assert response.context["units"][0]["archived"] and response.context["completed"] == 1
    assert revision.units.get().problem_version_id == version.pk
    revision.data["title"] = "Tampered"
    with pytest.raises(ValidationError):
        revision.save()


def test_invalid_finished_records_do_not_count_as_course_completion(
    catalog, org, user, other_user, client
):
    from test_showcase import job

    add_member(user, org, other_user, Role.LEARNER)
    revision = services.save_course(user, org.pk, "course", course_plan())
    services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    services.enroll(other_user, org.pk, revision.pk, "join")
    job(other_user, environment={})
    job(other_user, mode="run")
    job(other_user, evaluation_fingerprint="")
    client.force_login(other_user)
    assert client.get(url("course", org.pk, revision.course_id)).context["completed"] == 0


def test_withdrawn_course_and_removed_members_keep_own_record_deletion(
    catalog, org, user, other_user, client
):
    member = add_member(user, org, other_user, Role.LEARNER)
    revision = services.save_course(user, org.pk, "course", course_plan())
    services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    services.enroll(other_user, org.pk, revision.pk, "join")
    services.course_action(user, org.pk, revision.pk, revision.digest, "withdraw")
    client.force_login(other_user)
    assert client.get(url("course", org.pk, revision.course_id)).status_code == 404
    services.change_member(user, org.pk, member.pk)
    assert client.get(url("detail", org.pk)).status_code == 404
    assert client.post(url("enrollment", org.pk, revision.pk, "leave")).status_code == 302
    assert not CourseEnrollment.objects.exists()


def test_problem_withdrawal_hides_course_title_and_body(catalog, org, user, client):
    revision = services.save_course(user, org.pk, "course", course_plan())
    services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    version = revision.units.get().problem_version
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    client.force_login(user)
    response = client.get(url("course", org.pk, revision.course_id))
    assert response.context["units"][0]["problem"] is None
    assert version.data["title"] not in response.content.decode()
    assert (
        client.get(
            url("course_unit", org.pk, revision.course_id, revision.revision, "first")
        ).status_code
        == 404
    )


def test_package_http_submit_review_then_owner_publication(
    org, user, reviewer, third_user, payload, client
):
    original = set(Problem.objects.filter(published=True).values_list("pk", flat=True))
    client.force_login(user)
    upload = SimpleUploadedFile(
        "source.json", json.dumps(payload).encode(), content_type="application/json"
    )
    response = client.post(
        url("package_create", org.pk), {"slug": "basics", "payload": upload, "acknowledge": "on"}
    )
    assert response.status_code == 302
    revision = PackageRevision.objects.get()
    endpoint = url("package_revision", org.pk, revision.package_id, 1)
    public = url("public_package", revision.package_id)
    guest = Client()
    assert guest.get(public).status_code == 404
    client.force_login(third_user)
    assert client.get(endpoint).status_code == 404
    assert client.get(url("review", revision.pk)).status_code == 403
    client.force_login(reviewer)
    assert client.get(url("review", revision.pk)).status_code == 404
    client.force_login(user)
    assert (
        client.post(
            url("package_submit", org.pk, revision.pk), {"digest": revision.digest}
        ).status_code
        == 302
    )
    client.force_login(reviewer)
    assert client.get(url("review", revision.pk)).status_code == 200
    assert (
        client.post(
            url("review", revision.pk),
            {
                "digest": revision.digest,
                "action": "approve",
                "note": "Reviewed exact input",
                "acknowledge": "on",
            },
        ).status_code
        == 302
    )
    assert guest.get(public).status_code == 404
    publish(revision)
    assert guest.get(public).status_code == 200
    problem = Problem.objects.get(pk="sample-org--basics--positive-sum")
    assert problem.published and problem.current.data["concepts"] == ["sample-org--basics--concept"]
    body = guest.get(public).content.decode()
    assert payload["content"]["problems"][0]["reference"] not in body
    assert "payload" not in guest.get(public).context
    assert (
        set(Problem.objects.filter(pk__in=original, published=True).values_list("pk", flat=True))
        == original
    )


def test_own_organization_cannot_review_and_is_staff_is_not_permission(
    org, user, other_user, payload, client
):
    revision = packages.save_package(user, org.pk, "basics", payload)
    packages.submit_package(user, org.pk, revision.pk, revision.digest)
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="organizations", codename="review_packages")
    )
    with pytest.raises(PermissionDenied):
        packages.review_package(user, revision.pk, revision.digest, "approve", "Self review")
    other_user.is_staff = True
    other_user.save(update_fields=["is_staff"])
    client.force_login(other_user)
    assert client.get(url("review_queue")).status_code == 403
    assert (
        client.post(
            url("review", revision.pk),
            {"digest": revision.digest, "action": "approve", "note": "staff", "acknowledge": "on"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "tamper", ["author-role", "reviewer-permission", "review-digest", "payload", "withdrawal"]
)
def test_publication_rechecks_current_authorization_and_exact_snapshot(
    org, user, reviewer, payload, tamper
):
    revision = approved(user, org, reviewer, payload)
    if tamper == "author-role":
        Membership.objects.filter(organization=org, user=user).update(role=Role.LEARNER)
    elif tamper == "reviewer-permission":
        reviewer.user_permissions.clear()
    elif tamper == "review-digest":
        PackageRevision.objects.filter(pk=revision.pk).update(approved_digest="0" * 64)
    elif tamper == "payload":
        PackageRevision.objects.filter(pk=revision.pk).update(payload={})
    else:
        Package.objects.filter(pk=revision.package_id).update(
            state=Package.State.WITHDRAWAL_REQUESTED
        )
    with pytest.raises((services.OrganizationError, PermissionDenied)):
        publishing.publish_package(revision.pk, revision.digest)
    assert not ManagedObject.objects.exists()
    assert not Problem.objects.filter(pk__startswith="sample-org--").exists()


def test_second_package_and_official_content_survive_scoped_upgrade_and_withdrawal(
    org, user, reviewer, payload
):
    first = publish(approved(user, org, reviewer, payload, "first"))
    second = publish(approved(user, org, reviewer, payload, "second"))
    original_version = Problem.objects.get(pk="sample-org--first--positive-sum").current
    next_payload = copy.deepcopy(payload)
    next_payload["content"]["problems"][0].update(revision=2, statement="Updated statement")
    next_revision = publish(approved(user, org, reviewer, next_payload, "first"))
    original_version.refresh_from_db()
    assert original_version.data["statement"] != "Updated statement"
    assert Problem.objects.get(pk="sample-org--first--positive-sum").current.revision == 2
    packages.request_withdrawal(user, org.pk, first.package_id)
    call_command(
        "org_package_withdraw",
        str(first.package_id),
        digest=next_revision.digest,
        stdout=io.StringIO(),
    )
    assert not Problem.objects.get(pk="sample-org--first--positive-sum").published
    assert Problem.objects.get(pk="sample-org--second--positive-sum").published
    assert Problem.objects.get(pk="sum-positive").published
    assert PackageRevision.objects.get(pk=second.pk).state == PackageRevision.State.PUBLISHED
    with transaction.atomic():
        nodes, problems = publishing.official_import_scope({"nodes": [], "problems": []})
    assert "sample-org--first--positive-sum" in problems
    assert "sample-org--second--concept" in nodes
    with transaction.atomic(), pytest.raises(services.OrganizationError):
        publishing.official_import_scope(
            {"nodes": [], "problems": [{"id": "sample-org--second--positive-sum"}]}
        )


def test_immutable_problem_failure_rolls_back_all_content_and_snapshots(
    org, user, reviewer, payload
):
    publish(approved(user, org, reviewer, payload))
    old_nodes = dict(
        KnowledgeNode.objects.filter(pk__startswith="sample-org--").values_list("pk", "data")
    )
    old_snapshots = KnowledgeSnapshot.objects.count()
    changed = copy.deepcopy(payload)
    changed["content"]["nodes"][0]["title"] = "Would be partial node update"
    changed["content"]["problems"][0]["statement"] = "Attempt to rewrite revision one"
    revision = approved(user, org, reviewer, changed)
    with pytest.raises(services.OrganizationError):
        publishing.publish_package(revision.pk, revision.digest)
    assert (
        dict(KnowledgeNode.objects.filter(pk__startswith="sample-org--").values_list("pk", "data"))
        == old_nodes
    )
    assert KnowledgeSnapshot.objects.count() == old_snapshots
    assert Package.objects.get(pk=revision.package_id).current.revision == 1


@pytest.mark.parametrize("collision", ["problem", "node", "project"])
def test_first_publication_rejects_existing_unowned_identity(
    org, user, reviewer, payload, collision
):
    revision = approved(user, org, reviewer, payload)
    identity = "sample-org--basics--positive-sum"
    if collision == "problem":
        Problem.objects.create(id=identity)
    elif collision == "node":
        KnowledgeNode.objects.create(id=identity, kind="domain", title="Existing", data={})
    else:
        Project.objects.create(id=identity)
    with pytest.raises(services.OrganizationError):
        publishing.publish_package(revision.pk, revision.digest)
    assert not ManagedObject.objects.exists()


def test_explicit_external_dependencies_are_validated_but_never_claimed(
    org, user, reviewer, payload
):
    publish(approved(user, org, reviewer, payload, "base"))
    external = copy.deepcopy(payload)
    external["content"]["nodes"] = []
    external["content"]["problems"][0]["concepts"] = ["sample-org--base--concept"]
    for kind in ("domain", "concept"):
        node = KnowledgeNode.objects.get(pk=f"sample-org--base--{kind}")
        external["external"].append({"kind": "node", "id": node.pk, "digest": digest(node.data)})
    revision = publish(approved(user, org, reviewer, external, "extension"))
    assert ManagedObject.objects.filter(package=revision.package).count() == 1
    assert ManagedObject.objects.get(pk="sample-org--base--concept").package.slug == "base"
    bad = copy.deepcopy(external)
    bad["external"].pop(0)
    with pytest.raises(services.OrganizationError):
        packages.save_package(user, org.pk, "broken", bad)
    revision2 = approved(user, org, reviewer, external, "later")
    KnowledgeNode.objects.filter(pk="sample-org--base--concept").update(published=False)
    with pytest.raises(services.OrganizationError):
        publishing.publish_package(revision2.pk, revision2.digest)


@pytest.mark.parametrize(
    "change",
    [
        "unknown-ref",
        "oversized-id",
        "source-type",
        "extra-field",
        "duplicate-id",
        "negative-revision",
    ],
)
def test_invalid_package_inputs_are_atomic(org, user, payload, change):
    data = copy.deepcopy(payload)
    if change == "unknown-ref":
        data["content"]["problems"][0]["concepts"] = ["unknown"]
    elif change == "oversized-id":
        data["content"]["problems"][0]["id"] = "x" * 80
    elif change == "source-type":
        data["source_url"] = 1
    elif change == "extra-field":
        data["execute"] = "never"
    elif change == "duplicate-id":
        data["content"]["problems"][0]["id"] = "domain"
    else:
        data["content"]["problems"][0]["revision"] = -1
    with pytest.raises(services.OrganizationError):
        packages.save_package(user, org.pk, "bad", data)
    assert not Package.objects.exists() and not PackageRevision.objects.exists()


def test_course_and_package_exports_are_own_only_account_deletion_retains_org_snapshots(
    catalog, org, user, other_user, reviewer, payload
):
    add_member(user, org, other_user, Role.LEARNER)
    course = services.save_course(user, org.pk, "course", course_plan())
    services.course_action(user, org.pk, course.pk, course.digest, "publish")
    services.enroll(other_user, org.pk, course.pk, "join")
    revision = approved(user, org, reviewer, payload)
    assert not export_user_data(other_user)["package_contributions"]
    assert export_user_data(other_user)["course_enrollments"]
    assert not export_user_data(user)["course_enrollments"]
    assert export_user_data(user)["package_contributions"][0]["digest"] == revision.digest
    assert json.dumps(export_user_data(user))
    other_user.delete()
    assert not CourseEnrollment.objects.exists()
    user.delete()
    org.refresh_from_db()
    course.refresh_from_db()
    revision.refresh_from_db()
    assert org.created_by is None and course.author is None and revision.author is None
    assert CourseRevision.objects.exists() and PackageRevision.objects.exists()
    assert not Membership.objects.filter(organization=org).exists()
    assert (
        not OrganizationEvent.objects.filter(actor__isnull=False).exclude(actor=reviewer).exists()
    )
    call_command("organization_restore_owner", org.pk, reviewer.username, stdout=io.StringIO())
    assert Membership.objects.get(organization=org, user=reviewer).role == Role.OWNER


def test_mutations_require_post_login_csrf_and_rate_limit(org, user, client, monkeypatch):
    assert client.post(url("create"), {}).status_code == 302
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post(url("create"), {"namespace": "csrf", "name": "x"}).status_code == 403
    client.force_login(user)
    assert client.get(url("invite", org.pk)).status_code == 405
    monkeypatch.setattr("organizations.views.consume", lambda *args: False)
    assert client.post(url("create"), {"namespace": "other", "name": "x"}).status_code == 429
    assert Organization.objects.count() == 1


def test_qualified_ids_fit_eighty_characters_and_can_be_used_in_courses(user, reviewer, payload):
    org = services.create_organization(user, "a" * 24, "Bounded identity", "")
    changed = copy.deepcopy(payload)
    changed["content"]["problems"][0]["id"] = "c" * 28
    revision = publish(approved(user, org, reviewer, changed, "b" * 24))
    version = Problem.objects.get(pk="a" * 24 + "--" + "b" * 24 + "--" + "c" * 28).current
    assert len(version.problem_id) == 80
    plan = {
        "title": "Namespaced course",
        "summary": "Exact imported version",
        "units": [
            {
                "key": "unit",
                "title": "Unit",
                "description": "Description",
                "problem": version.problem_id,
                "revision": version.revision,
                "digest": version.digest,
            }
        ],
    }
    course = services.save_course(user, org.pk, "imported", plan)
    assert course.units.get().problem_version_id == version.pk
    before = OrganizationEvent.objects.count()
    publishing.publish_package(revision.pk, revision.digest)
    assert OrganizationEvent.objects.count() == before


def test_official_import_respects_reserved_namespace_before_any_package_is_published(org):
    with transaction.atomic(), pytest.raises(services.OrganizationError):
        publishing.official_import_scope(
            {"nodes": [{"id": org.pk + "--future--node"}], "problems": []}
        )


def test_reviewer_permission_command_does_not_grant_staff_or_other_permissions(third_user):
    call_command("organization_reviewer", third_user.username, grant=True, stdout=io.StringIO())
    third_user.refresh_from_db()
    assert not third_user.is_staff and not third_user.is_superuser
    assert set(third_user.user_permissions.values_list("content_type__app_label", "codename")) == {
        ("organizations", "review_packages")
    }
    call_command("organization_reviewer", third_user.username, revoke=True, stdout=io.StringIO())
    assert not third_user.user_permissions.exists()


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"not JSON", b" " * 75001])
def test_package_parser_rejects_duplicate_nonfinite_invalid_and_oversized_inputs(raw):
    with pytest.raises(services.OrganizationError):
        packages.parse_payload(raw)


def test_course_json_surrogates_are_rejected_without_partial_rows(catalog, org, user):
    plan = course_plan()
    plan["title"] = "\ud800"
    with pytest.raises(services.OrganizationError):
        services.save_course(user, org.pk, "invalid", plan)
    assert not Course.objects.exists()


def test_cancelled_approval_cannot_be_published(org, user, reviewer, payload, client):
    revision = approved(user, org, reviewer, payload)
    client.force_login(user)
    assert (
        client.post(
            url("package_cancel", org.pk, revision.pk), {"digest": revision.digest}
        ).status_code
        == 302
    )
    revision.refresh_from_db()
    assert revision.state == PackageRevision.State.WITHDRAWN and not revision.approved_digest
    with pytest.raises(services.OrganizationError):
        publishing.publish_package(revision.pk, revision.digest)
    assert not ManagedObject.objects.exists()
    with pytest.raises(ValidationError):
        revision.package.slug = "renamed"
        revision.package.save()


@pytest.mark.parametrize("namespace", ["new", "packages", "review", "invitations"])
def test_route_names_cannot_be_reserved_as_organizations(user, namespace):
    with pytest.raises(services.OrganizationError):
        services.create_organization(user, namespace, "Conflict", "")
    assert not Organization.objects.exists()


def test_course_unit_drift_cannot_change_pinned_publication_or_reading(catalog, org, user, client):
    revision = services.save_course(user, org.pk, "course", course_plan())
    services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    replacement = Problem.objects.get(pk="dot-product").current
    revision.units.update(problem_version=replacement)
    with pytest.raises(services.OrganizationError):
        services.course_action(user, org.pk, revision.pk, revision.digest, "publish")
    client.force_login(user)
    assert client.get(url("course", org.pk, revision.course_id)).status_code == 404
    assert (
        client.get(
            url("course_unit", org.pk, revision.course_id, revision.revision, "first")
        ).status_code
        == 404
    )


def test_official_full_import_preserves_other_packages_and_rejects_collisions(
    org, user, reviewer, payload, catalog
):
    from core.content import import_package

    revision = publish(approved(user, org, reviewer, payload))
    original = Problem.objects.get(pk="sample-org--basics--positive-sum").current
    import_package(catalog)
    assert Problem.objects.get(pk=original.problem_id).published
    assert KnowledgeNode.objects.get(pk="sample-org--basics--concept").published
    assert Package.objects.get(pk=revision.package_id).current_id == revision.pk
    changed = copy.deepcopy(catalog)
    changed["problems"][0]["id"] = "sample-org--basics--positive-sum"
    # Remove original paths so the package is valid before the ownership gate.
    changed["nodes"] = [item for item in changed["nodes"] if item["kind"] != "path"]
    with pytest.raises(services.OrganizationError):
        import_package(changed)
    assert Problem.objects.get(pk=original.problem_id).current_id == original.pk


def test_real_account_http_export_and_delete_keep_institutional_snapshots(
    org, user, other_user, reviewer, payload, client
):
    revision = approved(user, org, reviewer, payload)
    course = services.save_course(user, org.pk, "private-course", course_plan())
    services.course_action(user, org.pk, course.pk, course.digest, "publish")
    add_member(user, org, other_user, Role.LEARNER)
    services.enroll(other_user, org.pk, course.pk, "join")
    client.force_login(other_user)
    data = client.post(reverse("export_account")).json()["organizations"]
    assert data["course_enrollments"]
    assert data["package_contributions"] == []
    assert data["course_contributions"] == []
    assert (
        client.post(
            reverse("delete_account"), {"password": "Other-passphrase-9521", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    assert not CourseEnrollment.objects.filter(user_id=other_user.pk).exists()
    assert not Membership.objects.filter(user_id=other_user.pk).exists()
    client.force_login(user)
    data = client.post(reverse("export_account")).json()["organizations"]
    assert data["package_contributions"][0]["digest"] == revision.digest
    assert data["course_contributions"][0]["digest"] == course.digest
    assert (
        client.post(
            reverse("delete_account"), {"password": "Test-passphrase-9621", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    revision.refresh_from_db()
    course.refresh_from_db()
    org.refresh_from_db()
    assert revision.author_id is None and course.author_id is None and org.created_by_id is None
    assert OrganizationEvent.objects.filter(organization=org, actor=None).exists()
    assert not Membership.objects.filter(organization=org).exists()


def test_english_interfaces_and_original_private_metadata_are_explicit(
    org, user, reviewer, payload, client
):
    from django.utils.translation import override

    revision = approved(user, org, reviewer, payload)
    with override("en"):
        client.cookies["django_language"] = "en"
        client.force_login(user)
        for name, args, expected in [
            ("index", (), "My organizations"),
            ("create", (), "Create a private organization"),
            ("detail", (org.pk,), "Organization courses"),
            ("course_create", (org.pk,), "Course"),
            ("package_create", (org.pk,), "package"),
        ]:
            response = client.get(url(name, *args))
            assert response.status_code == 200
            assert expected in response.content.decode()
        response = client.get(url("package_revision", org.pk, revision.package_id, 1))
        assert response.status_code == 200
        assert "Trusted" in response.content.decode() or "trusted" in response.content.decode()
        client.force_login(reviewer)
        response = client.get(url("review", revision.pk))
        assert response.status_code == 200
        assert "digest" in response.content.decode()


def test_course_creation_offers_valid_visible_version_example(org, user, client, catalog):
    client.force_login(user)
    response = client.get(url("course_create", org.pk))
    plan = json.loads(response.context["form"].initial["plan"])
    assert services.validate_course(plan)
    hidden = plan["units"][0]["problem"]
    Problem.objects.filter(pk=hidden).update(published=False)
    response = client.get(url("course_create", org.pk))
    assert json.loads(response.context["form"].initial["plan"])["units"][0]["problem"] != hidden
