import json

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from localization.presentation import localized_problem
from projects.models import Project, ProjectRevision
from projects.presentation import localized_stage_cards


def revision_for(slug, number=None):
    project = get_object_or_404(
        Project.objects.select_related("current"), pk=slug, published=True, current__isnull=False
    )
    if number is None:
        return project, project.current
    if number > 2**31 - 1:
        raise Http404
    return project, get_object_or_404(ProjectRevision, project=project, revision=number)


def revision_card(project, revision):
    return {
        "id": project.pk,
        "revision": revision.revision,
        "title": revision.data["title"],
        "summary": revision.data["summary"],
        "integration_goal": revision.data["integration_goal"],
        "historical": project.current_id != revision.pk,
    }


@require_GET
@never_cache
def index(request):
    rows = Project.objects.filter(published=True, current__isnull=False).select_related("current")
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "projects/index.html",
        {"page": page, "projects": [revision_card(item, item.current) for item in page]},
    )


@require_GET
@never_cache
def detail(request, slug, number=None):
    project, revision = revision_for(slug, number)
    cards = localized_stage_cards(revision, request.user)
    return render(
        request,
        "projects/detail.html",
        {
            "project": revision_card(project, revision),
            "stages": cards,
            "completed": sum(card["evidence"] is not None for card in cards),
            "all_complete": all(card["evidence"] is not None for card in cards),
            "history": Paginator(project.revisions.only("revision"), 20).get_page(
                request.GET.get("page")
            ),
        },
    )


@require_GET
@never_cache
def stage(request, slug, number, key):
    project, revision = revision_for(slug, number)
    model = get_object_or_404(revision.stages.select_related("problem_version__problem"), key=key)
    card = next(
        item for item in localized_stage_cards(revision, request.user) if item["key"] == key
    )
    if not card["available"]:
        raise Http404
    data = localized_problem(model.problem_version)
    examples = [
        {
            "name": case["name"],
            "input": case["stdin"] if data["evaluator"] == "python-stdio-v1" else case["call"],
            "expected": json.dumps(case["expected"], ensure_ascii=False),
        }
        for case in data["examples"]
    ]
    # Render only this allowlist, never the raw version, tests or reference code.
    problem = {
        field: data[field]
        for field in (
            "title",
            "statement",
            "constraints",
            "language",
            "evaluator",
            "comparison",
            "outcomes",
            "content_language",
            "translation_fallback",
        )
    }
    problem["examples"] = examples
    return render(
        request,
        "projects/stage.html",
        {"project": revision_card(project, revision), "stage": card, "problem": problem},
    )
