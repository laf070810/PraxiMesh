"""Bounded, non-executable owner imports. Omitted projects are never implicitly withdrawn."""

import json
import re
from pathlib import Path

from django.db import transaction

from core.content import ContentError, digest
from core.models import ProblemVersion
from projects.models import Project, ProjectRevision, ProjectStage

SLUG = re.compile(r"[a-z][a-z0-9-]{0,79}")
HEX64 = re.compile(r"[a-f0-9]{64}")
MAX_PACKAGE_BYTES = 1_000_000


def text(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and "\0" not in value
    )


def unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContentError("Duplicate project JSON key.")
        value[key] = item
    return value


def invalid_number(value):
    raise ContentError("Nonfinite project JSON value.")


def load_package(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise ContentError("Project package exceeds 1 MB.")
    try:
        data = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_number)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContentError("Invalid project JSON.") from error
    validate_package(data)
    return data


def validate_package(package):
    if (
        not isinstance(package, dict)
        or set(package) != {"schema_version", "projects"}
        or type(package["schema_version"]) is not int
        or package["schema_version"] != 1
        or not isinstance(package["projects"], list)
        or not 1 <= len(package["projects"]) <= 100
    ):
        raise ContentError("Expected project schema_version 1 and 1–100 project plans.")
    ids = set()
    for plan in package["projects"]:
        if not isinstance(plan, dict) or set(plan) != {
            "id",
            "revision",
            "title",
            "summary",
            "integration_goal",
            "stages",
        }:
            raise ContentError("Project plan has missing or unknown fields.")
        if (
            not isinstance(plan["id"], str)
            or not SLUG.fullmatch(plan["id"])
            or plan["id"] in ids
            or type(plan["revision"]) is not int
            or not 1 <= plan["revision"] <= 2**31 - 1
            or not text(plan["title"], 160)
            or not text(plan["summary"], 2000)
            or not text(plan["integration_goal"], 4000)
            or not isinstance(plan["stages"], list)
            or not 2 <= len(plan["stages"]) <= 20
        ):
            raise ContentError("Invalid project identity, revision, text or stage count.")
        ids.add(plan["id"])
        stages, problems, finals = {}, set(), []
        for position, stage in enumerate(plan["stages"]):
            if not isinstance(stage, dict) or set(stage) != {
                "id",
                "title",
                "description",
                "problem",
                "problem_revision",
                "problem_digest",
                "recommended",
                "final",
            }:
                raise ContentError("Stage has missing or unknown fields.")
            if (
                not isinstance(stage["id"], str)
                or not SLUG.fullmatch(stage["id"])
                or stage["id"] in stages
                or not isinstance(stage["problem"], str)
                or not SLUG.fullmatch(stage["problem"])
                or stage["problem"] in problems
                or type(stage["problem_revision"]) is not int
                or not 1 <= stage["problem_revision"] <= 2**31 - 1
                or not isinstance(stage["problem_digest"], str)
                or not HEX64.fullmatch(stage["problem_digest"])
                or not text(stage["title"], 160)
                or not text(stage["description"], 4000)
                or type(stage["final"]) is not bool
                or not isinstance(stage["recommended"], list)
                or len(stage["recommended"]) > 19
                or any(not isinstance(key, str) for key in stage["recommended"])
                or len(set(stage["recommended"])) != len(stage["recommended"])
            ):
                raise ContentError("Invalid stage metadata, problem pin or recommendation list.")
            # Every recommendation points to an earlier stage. This also rejects
            # cycles while keeping the learning order optional in the UI.
            if not set(stage["recommended"]) <= stages.keys():
                raise ContentError("Recommendations must refer to distinct earlier stages.")
            stages[stage["id"]] = stage
            problems.add(stage["problem"])
            if stage["final"]:
                finals.append(position)
        if finals != [len(stages) - 1]:
            raise ContentError("Exactly the last stage must be the final integration task.")
        final = plan["stages"][-1]
        reachable, pending = set(), list(final["recommended"])
        while pending:
            key = pending.pop()
            if key not in reachable:
                reachable.add(key)
                pending.extend(stages[key]["recommended"])
        if reachable != stages.keys() - {final["id"]}:
            raise ContentError(
                "The final task must connect all component stages by recommendations."
            )
    try:
        if (
            len(json.dumps(package, ensure_ascii=False, allow_nan=False).encode())
            > MAX_PACKAGE_BYTES
        ):
            raise ContentError("Project package exceeds 1 MB.")
    except (ValueError, TypeError, RecursionError) as error:
        raise ContentError("Invalid project data.") from error


def resolve_versions(plan):
    versions = []
    for stage in plan["stages"]:
        version = (
            ProblemVersion.objects.select_related("problem")
            .filter(problem_id=stage["problem"], revision=stage["problem_revision"])
            .first()
        )
        if (
            version is None
            or version.digest != stage["problem_digest"]
            or not version.problem.published
            or version.problem.current_id is None
        ):
            raise ContentError("Stage refers to an unavailable or mismatched problem revision.")
        versions.append(version)
    return versions


@transaction.atomic
def import_package(package, *, check_only=False):
    validate_package(package)
    for plan in package["projects"]:
        versions = resolve_versions(plan)
        project = Project.objects.select_for_update().filter(pk=plan["id"]).first()
        revision = ProjectRevision.objects.filter(
            project_id=plan["id"], revision=plan["revision"]
        ).first()
        fingerprint = digest(plan)
        if revision and (revision.digest != fingerprint or revision.data != plan):
            raise ContentError("An immutable project revision cannot be changed.")
        if project and project.current_id and project.current.revision > plan["revision"]:
            raise ContentError("Cannot silently roll back a published project revision.")
        if revision:
            actual = list(
                revision.stages.values_list("key", "position", "problem_version_id", "data")
            )
            expected = [
                (stage["id"], i, version.pk, stage)
                for i, (stage, version) in enumerate(zip(plan["stages"], versions, strict=True))
            ]
            if actual != expected:
                raise ContentError("Stored project stage bindings differ from the immutable plan.")
        if check_only:
            continue
        project = project or Project.objects.create(pk=plan["id"])
        if revision is None:
            revision = ProjectRevision.objects.create(
                project=project, revision=plan["revision"], digest=fingerprint, data=plan
            )
            ProjectStage.objects.bulk_create(
                [
                    ProjectStage(
                        revision=revision,
                        key=stage["id"],
                        position=i,
                        problem_version=version,
                        data=stage,
                    )
                    for i, (stage, version) in enumerate(zip(plan["stages"], versions, strict=True))
                ]
            )
        project.current, project.published = revision, True
        project.save(update_fields=["current", "published"])
    return len(package["projects"])
