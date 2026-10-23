from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from challenges.models import Challenge, ChallengeRevision, Participation
from challenges.progress import STATES, state, task_cards
from learning.views import limit_writes
from localization.presentation import localized_problem


def revision_for(slug, number=None):
    challenge = get_object_or_404(
        Challenge.objects.select_related("current"), pk=slug, published=True, current__isnull=False
    )
    if number is None:
        return challenge, challenge.current
    if number > 2**31 - 1:
        raise Http404
    return challenge, get_object_or_404(ChallengeRevision, challenge=challenge, revision=number)


def revision_card(challenge, revision, now):
    status = state(revision, now)
    return {
        "id": challenge.pk,
        "revision": revision.revision,
        "title": revision.data["title"],
        "summary": revision.data["summary"],
        "rules": revision.data["rules"],
        "state": status,
        "state_label": STATES[status],
        "starts_at": revision.starts_at,
        "ends_at": revision.ends_at,
        "historical": challenge.current_id != revision.pk,
    }


@require_GET
@never_cache
def index(request):
    rows = Challenge.objects.filter(published=True, current__isnull=False).select_related("current")
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    now = timezone.now()
    return render(
        request,
        "challenges/index.html",
        {
            "page": page,
            "challenges": [revision_card(row, row.current, now) for row in page],
        },
    )


@require_GET
@login_required
@never_cache
def mine(request):
    rows = Participation.objects.filter(user=request.user).select_related("revision__challenge")
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    now = timezone.now()
    return render(
        request,
        "challenges/mine.html",
        {
            "page": page,
            "participations": [
                {
                    "id": row.revision.challenge_id,
                    "revision": row.revision.revision,
                    "joined_at": row.joined_at,
                    "available": row.revision.challenge.published,
                    "challenge": revision_card(row.revision.challenge, row.revision, now)
                    if row.revision.challenge.published
                    else None,
                }
                for row in page
            ],
        },
    )


@require_GET
@never_cache
def detail(request, slug, number=None):
    challenge, revision = revision_for(slug, number)
    participation = (
        Participation.objects.filter(user=request.user, revision=revision).first()
        if request.user.is_authenticated
        else None
    )
    now = timezone.now()
    cards = task_cards(revision, participation, now=now)
    return render(
        request,
        "challenges/detail.html",
        {
            "challenge": revision_card(challenge, revision, now),
            "tasks": cards,
            "participation": participation,
            "completed": sum(card["evidence"] is not None for card in cards),
            "can_join": challenge.current_id == revision.pk and now < revision.ends_at,
            "history": Paginator(challenge.revisions.only("revision"), 20).get_page(
                request.GET.get("page")
            ),
        },
    )


@require_POST
@login_required
@limit_writes
@never_cache
@transaction.atomic
def participate(request, slug, number):
    action = request.POST.get("action")
    if number > 2**31 - 1:
        raise Http404
    # Serialize account deletion and repeated joins; learner identity is never
    # accepted from POST data. Public definition tables remain SELECT-only.
    get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
    if action == "leave":
        Participation.objects.filter(
            user=request.user, revision__challenge_id=slug, revision__revision=number
        ).delete()
        messages.success(request, gettext("已退出活动。重新加入将从新的加入时间统计。"))
        return redirect("challenges:mine")
    if action != "join":
        return HttpResponse(gettext("请选择加入或退出活动。"), status=400)
    challenge, revision = revision_for(slug, number)
    if challenge.current_id != revision.pk or timezone.now() >= revision.ends_at:
        return HttpResponse(gettext("此修订已结束或已被更新，不能加入。"), status=409)
    Participation.objects.get_or_create(user=request.user, revision=revision)
    messages.success(request, gettext("已加入活动，参与记录与进度仅本人可见。"))
    return redirect("challenges:revision", slug=slug, number=number)


@require_GET
@never_cache
def task(request, slug, number, key):
    challenge, revision = revision_for(slug, number)
    model = get_object_or_404(revision.tasks.select_related("problem_version__problem"), key=key)
    if not model.problem_version.problem.published or not model.problem_version.problem.current_id:
        raise Http404
    data = localized_problem(model.problem_version)
    # localized_problem is the same public allowlist used by the practice API;
    # never pass the stored model/data (which contains hidden tests) to a template.
    return render(
        request,
        "challenges/task.html",
        {
            "challenge": revision_card(challenge, revision, timezone.now()),
            "task": {
                "title": model.data["title"],
                "description": model.data["description"],
                "problem_id": model.problem_version.problem_id,
                "problem_revision": model.problem_version.revision,
                "archived": model.problem_version.problem.current_id != model.problem_version_id,
            },
            "problem": data,
        },
    )
