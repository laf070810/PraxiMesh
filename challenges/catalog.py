"""Bounded declarative imports, available only through the trusted owner connection."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from django.db import transaction

from challenges.models import Challenge, ChallengeRevision, ChallengeTask
from core.content import ContentError, digest
from core.models import ProblemVersion
from organizations.publishing import publication_lock
from projects.catalog import HEX64, SLUG, text

MAX_PACKAGE_BYTES = 1_000_000
UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def utc_time(value):
    if not isinstance(value, str) or not UTC_TIME.fullmatch(value):
        raise ContentError("Challenge times must use YYYY-MM-DDTHH:MM:SSZ in UTC.")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ContentError("Invalid challenge time.") from error


def validate_package(package):
    if (
        not isinstance(package, dict)
        or set(package) != {"schema_version", "challenges"}
        or type(package["schema_version"]) is not int
        or package["schema_version"] != 1
        or not isinstance(package["challenges"], list)
        or not 1 <= len(package["challenges"]) <= 100
    ):
        raise ContentError("Expected challenge schema_version 1 and 1–100 activities.")
    ids = set()
    for plan in package["challenges"]:
        if not isinstance(plan, dict) or set(plan) != {
            "id",
            "revision",
            "title",
            "summary",
            "rules",
            "starts_at",
            "ends_at",
            "tasks",
        }:
            raise ContentError("Challenge has missing or unknown fields.")
        if (
            not isinstance(plan["id"], str)
            or not SLUG.fullmatch(plan["id"])
            or plan["id"] in ids
            or plan["id"] == "mine"
            or type(plan["revision"]) is not int
            or not 1 <= plan["revision"] <= 2**31 - 1
            or not text(plan["title"], 160)
            or not text(plan["summary"], 2000)
            or not text(plan["rules"], 4000)
            or not isinstance(plan["tasks"], list)
            or not 1 <= len(plan["tasks"]) <= 30
        ):
            raise ContentError("Invalid challenge identity, text, revision or task count.")
        ids.add(plan["id"])
        if utc_time(plan["starts_at"]) >= utc_time(plan["ends_at"]):
            raise ContentError("Challenge end must be later than its start.")
        keys, problems = set(), set()
        for task in plan["tasks"]:
            if not isinstance(task, dict) or set(task) != {
                "id",
                "title",
                "description",
                "problem",
                "problem_revision",
                "problem_digest",
            }:
                raise ContentError("Challenge task has missing or unknown fields.")
            if (
                not isinstance(task["id"], str)
                or not SLUG.fullmatch(task["id"])
                or task["id"] in keys
                or not isinstance(task["problem"], str)
                or not SLUG.fullmatch(task["problem"])
                or task["problem"] in problems
                or type(task["problem_revision"]) is not int
                or not 1 <= task["problem_revision"] <= 2**31 - 1
                or not isinstance(task["problem_digest"], str)
                or not HEX64.fullmatch(task["problem_digest"])
                or not text(task["title"], 160)
                or not text(task["description"], 4000)
            ):
                raise ContentError("Invalid challenge task or immutable problem pin.")
            keys.add(task["id"])
            problems.add(task["problem"])
    try:
        if (
            len(json.dumps(package, ensure_ascii=False, allow_nan=False).encode())
            > MAX_PACKAGE_BYTES
        ):
            raise ContentError("Challenge package exceeds 1 MB.")
    except (ValueError, TypeError, RecursionError) as error:
        raise ContentError("Invalid challenge data.") from error


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContentError("Duplicate challenge JSON key.")
        result[key] = value
    return result


def invalid_number(value):
    raise ContentError("Nonfinite challenge JSON value.")


def load_package(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise ContentError("Challenge package exceeds 1 MB.")
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=invalid_number
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContentError("Invalid UTF-8 challenge JSON.") from error
    validate_package(data)
    return data


@transaction.atomic
def import_package(package, *, check_only=False):
    validate_package(package)
    publication_lock()
    for plan in package["challenges"]:
        versions = []
        for task in plan["tasks"]:
            version = (
                ProblemVersion.objects.select_related("problem")
                .filter(problem_id=task["problem"], revision=task["problem_revision"])
                .first()
            )
            if (
                version is None
                or version.digest != task["problem_digest"]
                or not version.problem.published
                or version.problem.current_id is None
            ):
                raise ContentError(
                    "Challenge refers to an unavailable or mismatched problem revision."
                )
            versions.append(version)
        challenge = Challenge.objects.select_related("current").filter(pk=plan["id"]).first()
        revision = ChallengeRevision.objects.filter(
            challenge_id=plan["id"], revision=plan["revision"]
        ).first()
        fingerprint = digest(plan)
        starts_at, ends_at = utc_time(plan["starts_at"]), utc_time(plan["ends_at"])
        if revision:
            actual = list(
                revision.tasks.values_list("key", "position", "problem_version_id", "data")
            )
            expected = [
                (task["id"], i, version.pk, task)
                for i, (task, version) in enumerate(zip(plan["tasks"], versions, strict=True))
            ]
            if (
                revision.digest != fingerprint
                or revision.data != plan
                or revision.starts_at != starts_at
                or revision.ends_at != ends_at
                or actual != expected
            ):
                raise ContentError("An immutable challenge revision or its pins cannot be changed.")
        if challenge and challenge.current_id and challenge.current.revision > plan["revision"]:
            raise ContentError("Cannot silently roll back a published challenge revision.")
        if check_only:
            continue
        challenge = challenge or Challenge.objects.create(pk=plan["id"])
        if revision is None:
            revision = ChallengeRevision.objects.create(
                challenge=challenge,
                revision=plan["revision"],
                digest=fingerprint,
                data=plan,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            ChallengeTask.objects.bulk_create(
                [
                    ChallengeTask(
                        revision=revision,
                        key=task["id"],
                        position=i,
                        problem_version=version,
                        data=task,
                    )
                    for i, (task, version) in enumerate(zip(plan["tasks"], versions, strict=True))
                ]
            )
        challenge.current, challenge.published = revision, True
        challenge.save(update_fields=["current", "published"])
    return len(package["challenges"])


@transaction.atomic
def withdraw(slug, expected_digest):
    publication_lock()
    challenge = Challenge.objects.select_related("current").filter(pk=slug).first()
    if not challenge or not challenge.current_id or challenge.current.digest != expected_digest:
        raise ContentError("Challenge does not exist or its current digest differs.")
    challenge.published = False
    challenge.save(update_fields=["published"])
