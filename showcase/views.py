from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.limits import consume
from core.models import Submission
from localization.presentation import localized_problem
from showcase import forms, services
from showcase.models import PublicProfile, SharedResult


def guarded(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST" and not consume("showcase-write", str(request.user.pk), 30, 60):
            response = HttpResponse(gettext("操作过于频繁，请一分钟后重试。"), status=429)
            response["Retry-After"] = "60"
            return response
        try:
            return view(request, *args, **kwargs)
        except services.SharingError as error:
            return render(
                request, "showcase/error.html", {"error": error.message}, status=error.status
            )
        except ValidationError:
            return render(
                request,
                "showcase/error.html",
                {"error": gettext("填写内容或公开确认不符合要求。")},
                status=400,
            )

    return wrapped


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def settings(request):
    profile = PublicProfile.objects.filter(user=request.user).first()
    initial = {
        "revision": profile.revision if profile else 0,
        "public_name": profile.public_name if profile else "",
        "bio": profile.bio if profile else "",
        "is_public": profile.is_public if profile else False,
    }
    form = forms.ProfileForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        services.save_profile(request.user, form.cleaned_data)
        messages.success(request, gettext("公开主页设置已保存。评测结果需要逐条主动分享。"))
        return redirect("showcase:settings")
    return render(
        request,
        "showcase/settings.html",
        {"form": form, "profile": profile},
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@login_required
@guarded
def close(request):
    form = forms.RevisionForm(request.POST)
    if not form.is_valid():
        return HttpResponse(gettext("请刷新页面后重试。"), status=400)
    services.close_profile(request.user, form.cleaned_data["revision"])
    messages.success(
        request, gettext("主页已关闭，作品和榜单条目已撤回。独立公开的社区正文保持原状态。")
    )
    return redirect("showcase:settings")


@require_GET
@login_required
def mine(request):
    profile = PublicProfile.objects.filter(user=request.user).first()
    jobs = (
        Submission.objects.filter(user=request.user, mode="submit", status="finished")
        .select_related("version__problem", "showcase_entry")
        .defer("source")
    )
    page = Paginator(jobs, 20).get_page(request.GET.get("page"))
    rows = []
    for job in page:
        try:
            metrics = services.result_metrics(job)
        except services.SharingError:
            metrics = None
        rows.append(
            {
                "job": job,
                "entry": getattr(job, "showcase_entry", None),
                "metrics": metrics,
                "available": metrics is not None
                and job.version.problem.published
                and job.version.problem.current_id is not None,
            }
        )
    return render(request, "showcase/mine.html", {"rows": rows, "page": page, "profile": profile})


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def share(request, submission_id):
    job = get_object_or_404(
        Submission.objects.select_related("version__problem"), pk=submission_id, user=request.user
    )
    metrics = services.result_metrics(job)
    if not job.version.problem.published or job.version.problem.current_id is None:
        raise services.SharingError(gettext("题目已下线，不能公开这条结果。"))
    profile = PublicProfile.objects.filter(user=request.user).first()
    entry = SharedResult.objects.filter(submission=job).first()
    form = forms.ShareForm(
        request.POST if request.method == "POST" else None,
        initial={
            "profile_revision": profile.revision if profile else 1,
            "entry_revision": entry.revision if entry else 0,
        },
    )
    if request.method == "POST" and form.is_valid():
        entry = services.share(
            request.user,
            job.pk,
            profile_revision=form.cleaned_data["profile_revision"],
            entry_revision=form.cleaned_data["entry_revision"],
            confirmed=form.cleaned_data["confirm_public"],
        )
        return redirect("showcase:entry", pk=entry.pk)
    preview = {
        "title": localized_problem(job.version)["title"],
        "translation_fallback": localized_problem(job.version)["translation_fallback"],
        "problem_id": job.version.problem_id,
        "revision": job.version.revision,
        "historical": job.version.problem.current_id != job.version_id,
        "fingerprint": job.evaluation_fingerprint,
        **metrics,
    }
    return render(
        request,
        "showcase/share.html",
        {"preview": preview, "profile": profile, "form": form},
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@login_required
@guarded
def withdraw(request, pk):
    form = forms.RevisionForm(request.POST)
    if not form.is_valid():
        return HttpResponse(gettext("请刷新页面后重试。"), status=400)
    services.withdraw(request.user, pk, form.cleaned_data["revision"])
    messages.success(request, gettext("这条评测结果已从公开作品和榜单撤回。"))
    return redirect("showcase:mine")


@require_GET
@never_cache
def profile(request, username):
    item = get_object_or_404(
        PublicProfile.objects.select_related("user"),
        user__username=username,
        user__is_active=True,
        is_public=True,
    )
    page = Paginator(
        list(services.cards(services.public_entries().filter(submission__user=item.user))), 20
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "showcase/profile.html",
        {
            "public_profile": {
                "username": item.user.username,
                "public_name": item.public_name,
                "bio": item.bio,
            },
            "page": page,
        },
    )


@require_GET
@never_cache
def entry(request, pk):
    item = get_object_or_404(services.public_entries(), pk=pk)
    try:
        card = services.public_card(item)
    except services.SharingError:
        raise Http404 from None
    return render(request, "showcase/entry.html", {"entry": card})


@require_GET
@never_cache
def boards(request):
    page = Paginator(services.board_groups(), 20).get_page(request.GET.get("page"))
    return render(request, "showcase/boards.html", {"page": page})


@require_GET
@never_cache
def board(request, version_id, fingerprint):
    if version_id > 2**63 - 1 or not services.HEX64.fullmatch(fingerprint):
        raise Http404
    rows = services.ranked_results(
        services.public_entries().filter(
            submission__version_id=version_id, submission__evaluation_fingerprint=fingerprint
        )
    )
    if not rows:
        raise Http404
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(request, "showcase/board.html", {"group": rows[0], "page": page})
