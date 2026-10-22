"""Evidence for one immutable project revision, scoped to the requesting learner."""

from django.db.models import F

from core.models import Submission
from core.result_evidence import InvalidResult, verified_metrics


def stage_cards(revision, user):
    stages = list(revision.stages.select_related("problem_version__problem"))
    versions = {stage.problem_version_id: stage.problem_version for stage in stages}
    accepted = {}
    if user.is_authenticated:
        records = (
            Submission.objects.filter(
                user=user,
                version_id__in=versions,
                mode="submit",
                status="finished",
                verdict="accepted",
                started_at__isnull=False,
                finished_at__gte=F("started_at"),
            )
            .defer("source")
            .order_by("finished_at", "pk")
        )
        for record in records.iterator(chunk_size=200):
            record.version = versions[record.version_id]
            try:
                verified_metrics(record)
            except InvalidResult:
                continue
            accepted.setdefault(record.version_id, {"id": record.pk, "at": record.finished_at})
    names = {stage.key: stage.data["title"] for stage in stages}
    availability = {
        stage.key: stage.problem_version.problem.published
        and stage.problem_version.problem.current_id is not None
        for stage in stages
    }
    cards = []
    for stage in stages:
        version = stage.problem_version
        available = version.problem.published and version.problem.current_id is not None
        cards.append(
            {
                "key": stage.key,
                "position": stage.position + 1,
                "title": stage.data["title"],
                "description": stage.data["description"],
                "final": stage.data["final"],
                "recommended": [
                    {"key": key, "title": names[key], "available": availability[key]}
                    for key in stage.data["recommended"]
                ],
                "problem_id": version.problem_id,
                "problem_revision": version.revision,
                "problem_title": version.data["title"] if available else None,
                "available": available,
                "archived": version.problem.current_id != version.pk,
                "evidence": accepted.get(version.pk),
            }
        )
    return cards
