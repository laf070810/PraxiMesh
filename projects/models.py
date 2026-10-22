"""Owner-published project plans; evidence remains in private core.Submission rows."""

from django.db import models


class Project(models.Model):
    id = models.SlugField(primary_key=True, max_length=80)
    published = models.BooleanField(default=False)
    current = models.ForeignKey(
        "ProjectRevision", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        ordering = ["id"]


class ProjectRevision(models.Model):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["project", "revision"], name="projects_unique_revision")
        ]


class ProjectStage(models.Model):
    revision = models.ForeignKey(ProjectRevision, on_delete=models.CASCADE, related_name="stages")
    key = models.SlugField(max_length=80)
    position = models.PositiveSmallIntegerField()
    problem_version = models.ForeignKey("core.ProblemVersion", on_delete=models.PROTECT)
    data = models.JSONField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "key"], name="projects_unique_stage_key"),
            models.UniqueConstraint(
                fields=["revision", "position"], name="projects_unique_stage_position"
            ),
        ]
