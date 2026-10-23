"""Only the user's own memberships, invitations, enrollments and contributions."""

from organizations.models import (
    CourseEnrollment,
    CourseRevision,
    Invitation,
    Membership,
    PackageRevision,
)


def export_user_data(user):
    def rows(query, fields):
        output = []
        for values in query.values(*fields):
            output.append(
                {
                    key: value.isoformat()
                    if hasattr(value, "isoformat")
                    else str(value)
                    if key.endswith("id") and value is not None
                    else value
                    for key, value in values.items()
                }
            )
        return output

    return {
        "memberships": rows(
            Membership.objects.filter(user=user), ["organization_id", "role", "created_at"]
        ),
        "invitations": rows(
            Invitation.objects.filter(target=user),
            ["id", "organization_id", "role", "state", "created_at"],
        ),
        "course_enrollments": rows(
            CourseEnrollment.objects.filter(user=user), ["revision_id", "created_at"]
        ),
        "course_contributions": rows(
            CourseRevision.objects.filter(author=user),
            ["id", "course_id", "revision", "data", "digest", "created_at"],
        ),
        "package_contributions": rows(
            PackageRevision.objects.filter(author=user),
            [
                "id",
                "package_id",
                "revision",
                "payload",
                "digest",
                "state",
                "review_note",
                "created_at",
            ],
        ),
    }
