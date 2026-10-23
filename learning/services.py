from learning.models import Bookmark, Note, TutorialReading


def export_user_data(user):
    """Only this account's records; no problem tests, reference code or other users."""
    from challenges.progress import export_user_data as challenge_data
    from learning.achievements import export_user_data as achievement_data

    return {
        "challenges": challenge_data(user),
        "achievements": achievement_data(user),
        "bookmarks": [
            {"problem": row.problem_id, "created_at": row.created_at.isoformat()}
            for row in Bookmark.objects.filter(user=user).order_by("pk")
        ],
        "notes": [
            {
                "id": row.pk,
                "title": row.title,
                "body": row.body,
                "problem": row.version.problem_id if row.version_id else None,
                "revision": row.version.revision if row.version_id else None,
                "knowledge_node": row.node_id,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in Note.objects.filter(user=user).select_related("version").order_by("pk")
        ],
        "reading": [
            {
                "tutorial": row.tutorial_id,
                "completed_at": row.completed_at.isoformat(),
                "content_digest": row.content_digest,
                "self_reported": True,
            }
            for row in TutorialReading.objects.filter(user=user).order_by("pk")
        ],
    }
