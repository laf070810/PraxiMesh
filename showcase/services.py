"""Explicit sharing; public serializers never expose Submission or environment JSON."""

import re
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext

from core.models import Submission
from core.result_evidence import InvalidResult, verified_metrics
from judge.results import LABELS
from localization.presentation import VERDICTS as DISPLAY_VERDICTS
from localization.presentation import localized_problem
from showcase.models import PublicProfile, SharedResult

HEX64 = re.compile(r"[a-f0-9]{64}")
VERDICTS = frozenset(LABELS) - {"internal_error"}


class SharingError(Exception):
    def __init__(self, message, status=409):
        self.message, self.status = message, status
        super().__init__(message)


def result_metrics(job):
    # Sequential-test counts are comparable only within the same immutable
    # problem version and policy. New specialist metrics need separate review.
    try:
        metrics = verified_metrics(job)
    except InvalidResult:
        raise SharingError(gettext("这条记录缺少可核对的正式评测结果，暂不能公开。"), 400) from None
    return {**metrics, "label": DISPLAY_VERDICTS[job.verdict]}


def lock_profile(user):
    # The existing user row also serializes first-time profile creation, where
    # there is no profile row to lock yet. Every sharing transition uses this order.
    if not user.is_authenticated:
        raise PermissionDenied
    if not get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).exists():
        raise Http404
    return PublicProfile.objects.filter(user=user).first()


def expected_revision(actual, expected):
    if actual != expected:
        raise SharingError(gettext("公开状态已经改变，请刷新页面后重试。"))


def withdraw_all(user):
    SharedResult.objects.filter(submission__user=user, is_public=True).update(
        is_public=False, revision=F("revision") + 1, updated_at=timezone.now()
    )


@transaction.atomic
def save_profile(user, values):
    profile = lock_profile(user)
    expected_revision(profile.revision if profile else 0, values["revision"])
    profile = profile or PublicProfile(user=user, revision=0)
    profile.public_name, profile.bio = values["public_name"], values["bio"]
    profile.is_public = values["is_public"]
    if profile.is_public and (not profile.public_name.strip() or not values.get("confirm_public")):
        raise ValidationError(gettext("公开需要独立名称和明确确认。"))
    profile.revision += 1
    profile.full_clean()
    profile.save()
    if not profile.is_public:
        withdraw_all(user)
    return profile


@transaction.atomic
def close_profile(user, revision):
    profile = lock_profile(user)
    if profile is None:
        raise Http404
    expected_revision(profile.revision, revision)
    profile.is_public = False
    profile.revision += 1
    profile.save(update_fields=["is_public", "revision", "updated_at"])
    withdraw_all(user)


@transaction.atomic
def share(user, submission_id, *, profile_revision, entry_revision, confirmed):
    profile = lock_profile(user)
    if profile is None or not profile.is_public:
        raise SharingError(gettext("请先明确开启公开主页。"))
    expected_revision(profile.revision, profile_revision)
    try:
        job = Submission.objects.select_for_update().get(pk=submission_id, user=user)
    except Submission.DoesNotExist:
        raise Http404 from None
    if not confirmed:
        raise ValidationError(gettext("公开结果需要明确确认。"))
    if not job.version.problem.published or job.version.problem.current_id is None:
        raise SharingError(gettext("题目已经下线，不能公开这条结果。"))
    result_metrics(job)
    entry = SharedResult.objects.filter(submission=job).first()
    expected_revision(entry.revision if entry else 0, entry_revision)
    if entry is None:
        entry = SharedResult(submission=job, revision=0)
    entry.is_public = True
    entry.revision += 1
    entry.save()
    return entry


@transaction.atomic
def withdraw(user, entry_id, revision):
    lock_profile(user)
    try:
        entry = SharedResult.objects.get(pk=entry_id, submission__user=user)
    except SharedResult.DoesNotExist:
        raise Http404 from None
    expected_revision(entry.revision, revision)
    entry.is_public = False
    entry.revision += 1
    entry.save(update_fields=["is_public", "revision", "updated_at"])


def public_entries():
    return (
        SharedResult.objects.filter(
            is_public=True,
            submission__user__is_active=True,
            submission__user__showcase_profile__is_public=True,
            submission__mode="submit",
            submission__status="finished",
            submission__verdict__in=VERDICTS,
            submission__version__problem__published=True,
            submission__version__problem__current__isnull=False,
        )
        .select_related("submission__user__showcase_profile", "submission__version__problem")
        .defer("submission__source")
    )


def public_card(entry):
    job = entry.submission
    metrics = result_metrics(job)
    profile = job.user.showcase_profile
    return {
        "id": entry.pk,
        "username": job.user.username,
        "public_name": profile.public_name,
        "problem_id": job.version.problem_id,
        "title": localized_problem(job.version)["title"],
        "translation_fallback": localized_problem(job.version)["translation_fallback"],
        "version_id": job.version_id,
        "revision": job.version.revision,
        "historical": job.version.problem.current_id != job.version_id,
        "fingerprint": job.evaluation_fingerprint,
        "policy": job.environment["policy"],
        "shared_at": entry.created_at,
        **metrics,
    }


def cards(query):
    for entry in query.iterator(chunk_size=200):
        try:
            yield public_card(entry)
        except SharingError:
            # Historical corruption or a later administrative correction cannot
            # leave stale or unverified metrics in a public page or leaderboard.
            continue


def ranked_results(query):
    """One explicitly shared best result per person; equal progress has equal rank."""
    best = {}
    for card in cards(query):
        previous = best.get(card["username"])
        if previous is None or card["passed"] > previous["passed"]:
            best[card["username"]] = card
    rows = sorted(best.values(), key=lambda row: (-row["passed"], row["username"]))
    previous_score, rank = None, 0
    for position, row in enumerate(rows, 1):
        if row["passed"] != previous_score:
            rank, previous_score = position, row["passed"]
        row["rank"] = rank
    return rows


def board_groups():
    groups = {}
    participants = defaultdict(set)
    for card in cards(public_entries()):
        key = card["version_id"], card["fingerprint"]
        groups.setdefault(
            key,
            {
                key: card[key]
                for key in (
                    "version_id",
                    "revision",
                    "title",
                    "translation_fallback",
                    "problem_id",
                    "historical",
                    "fingerprint",
                    "policy",
                    "total",
                )
            },
        )
        participants[key].add(card["username"])
    return [
        {**group, "participants": len(participants[key])} for key, group in sorted(groups.items())
    ]


def export_user_data(user):
    profile = PublicProfile.objects.filter(user=user).first()
    return {
        "profile": {
            "is_public": profile.is_public,
            "public_name": profile.public_name,
            "bio": profile.bio,
            "revision": profile.revision,
            "updated_at": profile.updated_at.isoformat(),
        }
        if profile
        else None,
        "shared_results": [
            {
                "id": str(entry.pk),
                "submission_id": str(entry.submission_id),
                "is_public": entry.is_public,
                "revision": entry.revision,
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in SharedResult.objects.filter(submission__user=user).order_by(
                "created_at", "pk"
            )
        ],
    }
