import json
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.content import public_problem
from core.forms import ProfileForm, RecoveryForm, RegisterForm, SourceForm
from core.limits import consume
from core.models import AccountProfile, Draft, Enrollment, KnowledgeNode, Problem, Submission
from core.queue import AdmissionError, available, enqueue
from judge.backends import result_environment_label
from judge.results import LABELS


def graph():
    return {node.id: node.data for node in KnowledgeNode.objects.filter(published=True)}


def states(user):
    if not user.is_authenticated:
        return {}, set()
    jobs = Submission.objects.filter(user=user, mode="submit").exclude(verdict="internal_error")
    tried = set(jobs.values_list("version__problem_id", flat=True))
    accepted = set(
        jobs.filter(status="finished", verdict="accepted").values_list("version_id", flat=True)
    )
    return {key: "attempted" for key in tried}, accepted


def problem_cards(user):
    attempted, accepted = states(user)
    return [
        {
            **public_problem(p.current),
            "version_id": p.current_id,
            "state": "completed" if p.current_id in accepted else attempted.get(p.id, "new"),
        }
        for p in Problem.objects.filter(published=True).select_related("current")
        if p.current
    ]


@require_GET
def home(request):
    nodes = graph()
    return render(
        request,
        "home.html",
        {
            "domains": [n for n in nodes.values() if n["kind"] == "domain"],
            "paths": [n for n in nodes.values() if n["kind"] == "path"],
            "problem_count": Problem.objects.filter(published=True).count(),
        },
    )


@require_GET
def learn(request):
    nodes = graph()
    return render(request, "learn.html", {"nodes": nodes.values()})


@require_GET
def node_detail(request, slug):
    node = get_object_or_404(KnowledgeNode, id=slug, published=True)
    nodes = graph()
    cards = problem_cards(request.user)
    data = node.data
    related_nodes = [
        n for n in nodes.values() if slug in n["domains"] + n["topics"] + n["concepts"]
    ]
    concepts = {slug} if node.kind == "concept" else set(data["concepts"])
    if node.kind in {"domain", "topic"}:
        concepts |= {n["id"] for n in related_nodes if n["kind"] == "concept"}
    problems = [p for p in cards if concepts.intersection(p["concepts"])]
    edges = [{**edge, "node": nodes[edge["target"]]} for edge in data["relations"]]
    incoming = [n for n in nodes.values() if any(e["target"] == slug for e in n["relations"])]
    card_map = {p["id"]: p for p in cards}
    steps = [
        {
            **step,
            "item": card_map.get(step["target"])
            if step["kind"] == "problem"
            else nodes.get(step["target"]),
        }
        for step in data["steps"]
    ]
    steps = [step for step in steps if step["item"] is not None]
    exercise_steps = [s for s in steps if s["kind"] == "problem"]
    completed = sum(s["item"]["state"] == "completed" for s in exercise_steps)
    enrolled = (
        request.user.is_authenticated
        and Enrollment.objects.filter(user=request.user, path=node).exists()
    )
    return render(
        request,
        "node.html",
        {
            "node": data,
            "related": related_nodes,
            "linked_concepts": [nodes[c] for c in data["concepts"]],
            "problems": problems,
            "edges": edges,
            "incoming": incoming,
            "steps": steps,
            "exercise_count": len(exercise_steps),
            "completed": completed,
            "enrolled": enrolled,
        },
    )


@require_POST
@login_required
def enroll(request, slug):
    path = get_object_or_404(KnowledgeNode, id=slug, kind="path", published=True)
    if request.POST.get("action") == "leave":
        Enrollment.objects.filter(user=request.user, path=path).delete()
    else:
        Enrollment.objects.get_or_create(user=request.user, path=path)
    return redirect("node", slug=slug)


@require_GET
def practice(request):
    nodes = graph()
    cards = problem_cards(request.user)
    query = request.GET.get("q", "")[:100].strip().casefold()
    domain = request.GET.get("domain", "")
    topic = request.GET.get("topic", "")
    concept = request.GET.get("concept", "")
    path = request.GET.get("path", "")
    difficulty = request.GET.get("difficulty", "")
    state = request.GET.get("state", "")
    language = request.GET.get("language", "")
    for key, value in [("difficulty", difficulty), ("state", state), ("language", language)]:
        if value:
            cards = [p for p in cards if p[key] == value]
    if query:
        cards = [
            p
            for p in cards
            if query
            in " ".join(
                [p["title"], p["summary"], *p["tags"], *[nodes[c]["title"] for c in p["concepts"]]]
            ).casefold()
        ]
    if concept:
        cards = [p for p in cards if concept in p["concepts"]]
    if domain:
        cards = [p for p in cards if any(domain in nodes[c]["domains"] for c in p["concepts"])]
    if topic:
        cards = [p for p in cards if any(topic in nodes[c]["topics"] for c in p["concepts"])]
    if path:
        targets = {
            s["target"] for s in nodes.get(path, {}).get("steps", []) if s["kind"] == "problem"
        }
        cards = [p for p in cards if p["id"] in targets]
    pagination = Paginator(cards, 12).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "practice.html",
        {"page": pagination, "nodes": nodes.values(), "params": params.urlencode()},
    )


def current_problem(slug):
    problem = get_object_or_404(Problem.objects.select_related("current"), id=slug, published=True)
    if problem.current is None:
        from django.http import Http404

        raise Http404
    return problem


@require_GET
def problem_detail(request, slug):
    from judge.backends import configured_runtime_description

    problem = current_problem(slug)
    data = public_problem(problem.current)
    source = data["starter"]
    if request.user.is_authenticated:
        draft = Draft.objects.filter(user=request.user, version=problem.current).first()
        if draft:
            source = draft.source
    nodes = graph()
    return render(
        request,
        "problem.html",
        {
            "problem": data,
            "version_id": problem.current_id,
            "source": source,
            "concepts": [nodes[c] for c in data["concepts"]],
            "judge_available": available(),
            "runtime_description": configured_runtime_description(),
        },
    )


@require_POST
@login_required
def save_draft(request, slug):
    problem = current_problem(slug)
    source = request.POST.get("source", "")
    if request.POST.get("version") != str(problem.current_id):
        return JsonResponse({"error": "题目已更新，请刷新页面。旧版本草稿仍保留。"}, status=409)
    if len(source) > 20000:
        return JsonResponse({"error": "代码不能超过 20000 字符。"}, status=400)
    if not consume("draft", str(request.user.pk), 60, 60):
        return JsonResponse({"error": "保存过于频繁，请稍后重试。"}, status=429)
    Draft.objects.update_or_create(
        user=request.user, version=problem.current, defaults={"source": source}
    )
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"saved": True})
    messages.success(request, "草稿已私密保存。")
    return redirect("problem", slug=slug)


@require_GET
@login_required
def draft_detail(request, draft_id):
    draft = get_object_or_404(
        Draft.objects.select_related("version__problem"), id=draft_id, user=request.user
    )
    problem = draft.version.problem
    return render(
        request,
        "draft.html",
        {
            "draft": draft,
            "problem": public_problem(draft.version),
            "current_available": problem.published and problem.current_id is not None,
            "is_current": problem.current_id == draft.version_id,
        },
    )


@require_POST
@login_required
def submit(request, slug):
    problem = current_problem(slug)
    form = SourceForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"error": "请提供不超过 20000 字符的 Python 代码及有效的提交方式。"}, status=400
        )
    if request.POST.get("version") != str(problem.current_id):
        return JsonResponse({"error": "题目版本已更新，请刷新后重试。"}, status=409)
    if not consume("submit", str(request.user.pk), 12, 60):
        return JsonResponse({"error": "提交过于频繁，请稍后再试。"}, status=429)
    try:
        job = enqueue(
            request.user, problem.current, form.cleaned_data["source"], form.cleaned_data["mode"]
        )
    except AdmissionError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    Draft.objects.update_or_create(
        user=request.user, version=problem.current, defaults={"source": form.cleaned_data["source"]}
    )
    if request.headers.get("Accept") == "application/json":
        from django.urls import reverse

        return JsonResponse(
            {"id": str(job.id), "url": reverse("submission", args=[job.id])}, status=202
        )
    return redirect("submission", job_id=job.id)


@require_GET
@login_required
def submission_detail(request, job_id):
    job = get_object_or_404(
        Submission.objects.select_related("version"), id=job_id, user=request.user
    )
    return render(
        request,
        "submission.html",
        {
            "job": job,
            "problem": public_problem(job.version),
            "verdict_label": LABELS.get(job.verdict, ""),
            "pending": job.status != "finished",
            "evaluation_image": result_environment_label(job.environment),
        },
    )


@require_GET
@login_required
def submission_status(request, job_id):
    job = get_object_or_404(Submission, id=job_id, user=request.user)
    return JsonResponse(
        {
            "status": job.status,
            "status_label": job.get_status_display(),
            "verdict": job.verdict,
            "label": LABELS.get(job.verdict, ""),
        }
    )


@require_GET
@login_required
def dashboard(request):
    jobs = Submission.objects.filter(user=request.user).select_related("version")
    cards = problem_cards(request.user)
    return render(
        request,
        "dashboard.html",
        {
            "page": Paginator(jobs, 20).get_page(request.GET.get("page")),
            "completed": sum(p["state"] == "completed" for p in cards),
            "enrollments": Enrollment.objects.filter(
                user=request.user, path__published=True
            ).select_related("path"),
            "drafts": Paginator(
                Draft.objects.filter(user=request.user)
                .select_related("version")
                .order_by("-updated_at", "-pk"),
                20,
            ).get_page(request.GET.get("draft_page")),
        },
    )


@require_http_methods(["GET", "POST"])
def register(request):
    if not settings.REGISTRATION_OPEN:
        return render(
            request,
            "message.html",
            {"title": "注册暂未开放", "body": "本站暂不接受新账号。公开知识和题目仍可浏览。"},
            status=403,
        )
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = secrets.token_urlsafe(32)
        with transaction.atomic():
            user = form.save()
            AccountProfile.objects.create(user=user, recovery_hash=make_password(code))
        login(request, user)
        return render(request, "recovery_code.html", {"code": code})
    return render(
        request,
        "account_form.html",
        {
            "form": form,
            "title": "创建学习账号",
            "button": "注册",
            "intro": "只需要用户名与密码。代码和学习记录默认仅你可见。",
        },
    )


@require_http_methods(["GET", "POST"])
def recover(request):
    form = RecoveryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            profile = (
                AccountProfile.objects.select_for_update()
                .select_related("user")
                .filter(user__username=form.cleaned_data["username"])
                .first()
            )
            valid = (
                check_password(form.cleaned_data["recovery_code"], profile.recovery_hash)
                if profile
                else check_password(
                    form.cleaned_data["recovery_code"], make_password("unavailable")
                )
            )
            if profile and valid and profile.user.is_active:
                user = profile.user
                user.set_password(form.cleaned_data["password"])
                user.save(update_fields=["password"])
                code = secrets.token_urlsafe(32)
                profile.recovery_hash = make_password(code)
                profile.save(update_fields=["recovery_hash"])
                login(request, user)
                return render(request, "recovery_code.html", {"code": code})
        form.add_error(None, "用户名或恢复码无效。")
    return render(
        request,
        "account_form.html",
        {
            "form": form,
            "title": "恢复账号",
            "button": "重置密码",
            "intro": "使用注册时保存的恢复码。成功后旧码作废，原登录会话失效。",
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def account(request):
    profile, _ = AccountProfile.objects.get_or_create(
        user=request.user, defaults={"recovery_hash": make_password(None)}
    )
    form = ProfileForm(request.POST or None, initial={"display_name": profile.display_name})
    if request.method == "POST" and form.is_valid():
        profile.display_name = form.cleaned_data["display_name"]
        profile.save(update_fields=["display_name"])
        messages.success(request, "个人资料已保存。")
        return redirect("account")
    return render(request, "account.html", {"form": form})


@require_POST
@login_required
def rotate_recovery(request):
    if not request.user.check_password(request.POST.get("password", "")):
        return HttpResponse("密码不正确。", status=400)
    code = secrets.token_urlsafe(32)
    AccountProfile.objects.update_or_create(
        user=request.user, defaults={"recovery_hash": make_password(code)}
    )
    return render(request, "recovery_code.html", {"code": code})


@require_POST
@login_required
def export_account(request):
    jobs = Submission.objects.filter(user=request.user).select_related("version")
    profile = AccountProfile.objects.filter(user=request.user).first()
    data = {
        "username": request.user.username,
        "display_name": profile.display_name if profile else "",
        "submissions": [
            {
                "id": str(j.id),
                "problem": j.version.problem_id,
                "revision": j.version.revision,
                "problem_digest": j.version.digest,
                "source": j.source,
                "mode": j.mode,
                "status": j.status,
                "verdict": j.verdict,
                "result": j.result,
                "evaluation_image": result_environment_label(j.environment),
                "evaluation_fingerprint": j.evaluation_fingerprint,
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ],
        "drafts": list(
            Draft.objects.filter(user=request.user).values(
                "version__problem_id", "version__revision", "source"
            )
        ),
        "paths": list(
            Enrollment.objects.filter(user=request.user).values_list("path_id", flat=True)
        ),
    }
    response = HttpResponse(
        json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json"
    )
    response["Content-Disposition"] = 'attachment; filename="praximesh-data.json"'
    return response


@require_POST
@login_required
def delete_account(request):
    if (
        not request.user.check_password(request.POST.get("password", ""))
        or request.POST.get("confirm") != "DELETE"
    ):
        return HttpResponse("请输入当前密码，并输入 DELETE 确认删除。", status=400)
    user = request.user
    logout(request)
    user.delete()
    return render(
        request,
        "message.html",
        {
            "title": "账号已删除",
            "body": "账号、草稿、提交记录和路径记录已从当前数据库删除。备份按站点保留周期清除。",
        },
    )


@require_GET
def health(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse(
        {"status": "ok", "evaluation": "available" if available() else "unavailable"}
    )
