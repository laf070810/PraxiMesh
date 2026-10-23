from coaching.models import DailyUsage, HintRequest, LearningPlan


def export_user_data(user):
    hints = []
    for item in HintRequest.objects.filter(user=user).select_related("version__problem"):
        visible = item.version.problem.published and item.version.problem.current_id is not None
        hints.append(
            {
                "id": str(item.pk),
                "problem": item.version.problem_id,
                "revision": item.version.revision,
                "state": item.state,
                "request_digest": item.request_digest,
                "configuration_digest": item.configuration_digest,
                "prompt_version": item.prompt_version,
                "model": item.model,
                "payload": item.payload if visible else None,
                "response": item.response if visible else None,
                "content_withheld": not visible,
                "error_code": item.error_code,
                "created_at": item.created_at.isoformat(),
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            }
        )
    return {
        "plans": [
            {
                "id": str(p.pk),
                "targets": p.targets,
                "suggestions": p.suggestions,
                "skipped": p.skipped,
                "active": p.active,
                "created_at": p.created_at.isoformat(),
            }
            for p in LearningPlan.objects.filter(user=user)
        ],
        "hints": hints,
        "usage": [
            {"day": u.day.isoformat(), "count": u.count}
            for u in DailyUsage.objects.filter(user=user).order_by("day")
        ],
    }
