import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from coaching import configuration, evidence, forms, hints, plans
from coaching.models import HintRequest, LearningPlan
from coaching.provider import ProviderError
from core.limits import consume
from core.models import Submission
from core.result_evidence import InvalidResult, verified_evidence
from localization.presentation import VERDICTS, localized_problem


def limited(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST" and not consume("coaching-write", str(request.user.pk), 30, 60):
            return HttpResponse(_("操作过于频繁，请一分钟后重试。"), status=429)
        return view(request, *args, **kwargs)

    return wrapped


@require_GET
@login_required
def index(request):
    current = LearningPlan.objects.filter(user=request.user, active=True).first()
    return render(
        request,
        "coaching/index.html",
        {
            "current": current,
            "history": Paginator(LearningPlan.objects.filter(user=request.user), 10).get_page(
                request.GET.get("page")
            ),
            "hints": Paginator(HintRequest.objects.filter(user=request.user), 10).get_page(
                request.GET.get("hints_page")
            ),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@limited
def goals(request):
    form = forms.GoalForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            plan = plans.create(request.user, form.cleaned_data["targets"])
        except plans.PlanError:
            form.add_error(None, _("目标内容已改变，请重新选择当前可见的概念。"))
        else:
            return redirect("coaching:plan", pk=plan.pk)
    return render(
        request,
        "coaching/goals.html",
        {"form": form},
        status=400 if request.method == "POST" else 200,
    )


@require_GET
@login_required
def plan(request, pk):
    item = get_object_or_404(LearningPlan, pk=pk, user=request.user)
    return render(request, "coaching/plan.html", plans.present(item))


@require_POST
@login_required
@limited
def plan_action(request, pk, action):
    try:
        value = request.POST.get("version")
        version_id = int(value) if value and len(value) < 20 else None
        plans.action(request.user, pk, action, version_id)
    except (ValueError, plans.PlanError):
        return HttpResponse(_("这个计划操作已不可用。"), status=409)
    return redirect(
        "coaching:index" if action == "delete" else "coaching:plan",
        **({} if action == "delete" else {"pk": pk}),
    )


@require_GET
@login_required
def analysis(request):
    data = evidence.build(request.user)
    data["cards"] = Paginator(data["cards"], 20).get_page(request.GET.get("page"))
    return render(request, "coaching/analysis.html", data)


@require_GET
@login_required
def diagnosis(request, pk):
    job = get_object_or_404(
        Submission.objects.select_related("version__problem"), pk=pk, user=request.user
    )
    if not job.version.problem.published:
        raise Http404
    try:
        verified_evidence(job)
    except InvalidResult:
        return render(request, "coaching/diagnosis.html", {"verified": False})
    # Do not infer hidden input. The existing result projection exposes only
    # worker-sanitized public cases; copy an explicit learner-safe whitelist.
    public = localized_problem(job.version)
    from core.content import public_problem

    names = {
        original["name"]: translated["name"]
        for original, translated in zip(
            public_problem(job.version)["examples"], public["examples"], strict=True
        )
    }
    cases = [
        {"name": names[case["name"]], "label": VERDICTS[case["verdict"]]}
        for case in job.result.get("public_cases", [])
        if isinstance(case, dict) and case.get("name") in names and case.get("verdict") in VERDICTS
    ]
    return render(
        request,
        "coaching/diagnosis.html",
        {
            "verified": True,
            "job": job,
            "label": VERDICTS[job.verdict],
            "problem": localized_problem(job.version),
            "cases": cases,
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@limited
def hint_create(request, version_id):
    version = hints._visible_version(version_id)
    problem = localized_problem(version)
    config = None
    try:
        config = configuration.current()
    except ProviderError:
        pass
    form = forms.HintForm(
        request.POST if request.method == "POST" else None,
        levels=len(problem["hints"]),
        initial={"hint_level": 0},
    )
    if request.method == "POST" and config and form.is_valid():
        try:
            item = hints.preview(request.user, version_id, **form.cleaned_data)
        except (ProviderError, hints.HintError):
            form.add_error(None, _("无法创建这份预览。请检查长度或删除未发送的旧预览。"))
        else:
            return redirect("coaching:hint", pk=item.pk)
    return render(
        request,
        "coaching/hint_form.html",
        {"form": form, "problem": problem, "config": config, "used": hints.usage(request.user)},
        status=400 if request.method == "POST" else 200,
    )


@require_GET
@login_required
def hint(request, pk):
    item = hints.owned(request.user, pk)
    data = hints.display(item)
    data["form"] = forms.ConfirmForm(initial={"digest": item.request_digest})
    return render(request, "coaching/hint.html", data)


@require_POST
@login_required
@limited
def hint_send(request, pk):
    hints.owned(request.user, pk)
    form = forms.ConfirmForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("请明确确认发送已预览的内容。"), status=400)
    try:
        item = hints.send(request.user, pk, form.cleaned_data["digest"])
    except ProviderError:
        return HttpResponse(_("辅导服务当前未启用或不可用。普通练习不受影响。"), status=503)
    except hints.HintError as error:
        reasons = {
            "quota": _("今日提示次数已用完，删除会话不会重置当日配额。"),
            "busy": _("辅导服务正在处理其他请求，请稍后重试。"),
            "preview_changed": _("提供方配置或预览已改变，请重新创建预览。"),
        }
        return HttpResponse(
            reasons.get(str(error), _("辅导服务暂不可用。")),
            status=429 if str(error) in {"quota", "busy"} else 409,
        )
    if item is None:
        raise Http404
    return redirect("coaching:hint", pk=item.pk)


@require_POST
@login_required
@limited
def hint_delete(request, pk):
    hints.remove(request.user, pk)
    messages.success(request, _("私有会话已删除；已发送的调用仍计入今日配额。"))
    return redirect("coaching:index")


@require_GET
@login_required
def plan_task(request, pk, version_id):
    item = get_object_or_404(LearningPlan, pk=pk, user=request.user)
    bound = next((row for row in item.suggestions if row["version_id"] == version_id), None)
    if bound is None:
        raise Http404
    version = hints._visible_version(version_id)
    if version.digest != bound["digest"]:
        raise Http404
    return render(
        request,
        "coaching/task.html",
        {
            "plan": item,
            "problem": localized_problem(version),
            "version_id": version.pk,
            "examples": [
                {"name": case["name"], "data": json.dumps(case, ensure_ascii=False, indent=2)}
                for case in localized_problem(version)["examples"]
            ],
            "archived": version.problem.current_id != version.pk,
        },
    )
