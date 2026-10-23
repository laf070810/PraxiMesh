"""Private, verified evidence for a participant's immutable activity window."""

from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from challenges.models import Participation
from core.models import Submission
from core.result_evidence import InvalidResult, verified_evidence
from localization.presentation import localized_problem

STATES = {"upcoming": _("未开始"), "active": _("进行中"), "ended": _("已结束")}


def state(revision, now):
    return (
        "upcoming" if now < revision.starts_at else "ended" if now >= revision.ends_at else "active"
    )


def task_cards(revision, participation=None, *, now=None):
    now = now or timezone.now()
    tasks = list(revision.tasks.select_related("problem_version__problem"))
    versions = {
        task.problem_version_id: task.problem_version
        for task in tasks
        if task.problem_version.problem.published and task.problem_version.problem.current_id
    }
    accepted = {}
    if participation is not None:
        if participation.revision_id != revision.pk:
            raise ValueError("Participation must belong to the displayed revision.")
        records = (
            Submission.objects.filter(
                user_id=participation.user_id,
                version_id__in=versions,
                mode="submit",
                status="finished",
                verdict="accepted",
                created_at__gte=max(participation.joined_at, revision.starts_at),
                created_at__lt=revision.ends_at,
                started_at__gte=F("created_at"),
                finished_at__gte=F("started_at"),
                finished_at__lt=revision.ends_at,
                finished_at__lte=now,
            )
            .defer("source")
            .order_by("finished_at", "pk")
        )
        for record in records.iterator(chunk_size=100):
            record.version = versions[record.version_id]
            try:
                verified_evidence(record)
            except InvalidResult:
                continue
            accepted.setdefault(record.version_id, {"id": record.pk, "at": record.finished_at})
    return [
        {
            "key": task.key,
            "position": task.position + 1,
            "title": task.data["title"],
            "description": task.data["description"],
            "problem_id": task.problem_version.problem_id,
            "problem_revision": task.problem_version.revision,
            "problem_title": localized_problem(task.problem_version)["title"]
            if task.problem_version_id in versions
            else None,
            "available": task.problem_version_id in versions,
            "archived": task.problem_version.problem.current_id != task.problem_version_id,
            "evidence": accepted.get(task.problem_version_id),
        }
        for task in tasks
    ]


def export_user_data(user):
    return [
        {
            "challenge": row.revision.challenge_id,
            "revision": row.revision.revision,
            "revision_digest": row.revision.digest,
            "joined_at": row.joined_at.isoformat(),
        }
        for row in Participation.objects.filter(user=user).select_related("revision").order_by("pk")
    ]
