"""Read-only evidence for the requesting learner; no teacher or cohort projection."""

from core.models import Submission
from core.result_evidence import InvalidResult, verified_evidence
from localization.presentation import localized_problem


def own_progress(revision, user, *, include_evidence=True):
    units = list(revision.units.select_related("problem_version__problem"))
    versions = {unit.problem_version_id: unit.problem_version for unit in units}
    accepted = {}
    for job in (
        Submission.objects.filter(
            user=user,
            version_id__in=versions if include_evidence else [],
            mode="submit",
            status="finished",
            verdict="accepted",
        )
        .defer("source")
        .order_by("finished_at", "pk")
    ):
        job.version = versions[job.version_id]
        try:
            verified_evidence(job)
        except InvalidResult:
            continue
        accepted.setdefault(job.version_id, {"id": job.pk, "at": job.finished_at})
    result = []
    for unit in units:
        version = unit.problem_version
        available = version.problem.published and version.problem.current_id is not None
        result.append(
            {
                "key": unit.key,
                "title": unit.title,
                "description": unit.description,
                "available": available,
                "version_number": version.revision,
                "problem": localized_problem(version) if available else None,
                "archived": version.pk != version.problem.current_id,
                "evidence": accepted.get(version.pk),
            }
        )
    return result
