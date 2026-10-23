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
from django.utils.translation import gettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from coaching.export import export_user_data as export_coaching
from community.services import export_user_data as export_community
from core.content import digest
from core.discovery import EVALUATORS, LIBRARIES, RUNTIMES, choices, filter_resources, metadata
from core.forms import ProfileForm, RecoveryForm, RegisterForm, SourceForm
from core.limits import consume
from core.models import AccountProfile, Draft, Enrollment, KnowledgeNode, Problem, Submission
from core.queue import AdmissionError, available, enqueue
from core.resources import reservation_data, reservation_units, usage
from core.result_evidence import SPECIALISTS, evidence_jobs, is_formal_evidence, safe_result
from judge.backends import result_environment_label
from learning.models import Bookmark, TutorialReading
from learning.services import export_user_data as export_learning
from localization.presentation import VERDICTS as LABELS
from localization.presentation import localized_node, localized_public_cases
from localization.presentation import localized_problem as public_problem
from organizations.export import export_user_data as export_organizations
from practice_api.models import AccessToken
from practice_api.services import export_user_data as export_practice_api
from showcase.services import export_user_data as export_showcase


def graph():
    return {node.id: localized_node(node) for node in KnowledgeNode.objects.filter(published=True)}


def states(user):
    if not user.is_authenticated:
        return {}, set()
    tried, accepted = set(), set()
    for job in evidence_jobs(user).iterator(chunk_size=100):
        if is_formal_evidence(job):
            tried.add(job.version.problem_id)
            if job.verdict == "accepted":
                accepted.add(job.version_id)
    return {key: "attempted" for key in tried}, accepted


def problem_cards(user):
    attempted, accepted = states(user)
    return [
        {
            **public_problem(p.current),
            **metadata(p.current),
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
    data = localized_node(node)
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
    reading = None
    if request.user.is_authenticated and node.kind == "tutorial":
        reading = TutorialReading.objects.filter(user=request.user, tutorial=node).first()
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
            "reading": reading,
            "reading_current": reading is not None and reading.content_digest == digest(node.data),
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
    options = {
        "evaluator_choices": choices(cards, "evaluator", EVALUATORS),
        "runtime_choices": choices(cards, "runtime", RUNTIMES),
        "library_choices": choices(cards, "library", LIBRARIES),
    }
    cards = filter_resources(cards, request.GET)
    query = request.GET.get("q", "")[:100].strip().casefold()
    domain = request.GET.get("domain", "")
    topic = request.GET.get("topic", "")
    concept = request.GET.get("concept", "")
    path = request.GET.get("path", "")
    difficulty = request.GET.get("difficulty", "")
    state = request.GET.get("state", "")
    language = request.GET.get("language", "")
    evaluator = request.GET.get("evaluator", "")
    if evaluator:
        cards = [p for p in cards if p["evaluator"] == evaluator]
    for key, value in [("difficulty", difficulty), ("state", state), ("language", language)]:
        if value:
            cards = [p for p in cards if p[key] == value]
    if query:
        cards = [
            p
            for p in cards
            if query
            in " ".join(
                [
                    p["title"],
                    p["summary"],
                    p["source_search"],
                    *p["tags"],
                    *[nodes[c]["title"] + " " + nodes[c]["source_search"] for c in p["concepts"]],
                ]
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
        {"page": pagination, "nodes": nodes.values(), "params": params.urlencode(), **options},
    )


def current_problem(slug):
    problem = get_object_or_404(Problem.objects.select_related("current"), id=slug, published=True)
    if problem.current is None:
        from django.http import Http404

        raise Http404
    return problem


@require_GET
def problem_detail(request, slug):
    from localization.presentation import runtime_description

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
            "cpu_budget": usage(request.user) if request.user.is_authenticated else None,
            "run_units": reservation_units(problem.current, "run"),
            "submit_units": reservation_units(problem.current, "submit"),
            "concepts": [nodes[c] for c in data["concepts"]],
            "judge_available": available(problem.current),
            "runtime_description": runtime_description(problem.current),
            "bookmarked": request.user.is_authenticated
            and Bookmark.objects.filter(user=request.user, problem=problem).exists(),
        },
    )


@require_POST
@login_required
def save_draft(request, slug):
    problem = current_problem(slug)
    source = request.POST.get("source", "")
    if request.POST.get("version") != str(problem.current_id):
        return JsonResponse(
            {"error": gettext("题目已更新，请刷新页面。旧版本草稿仍保留。")}, status=409
        )
    if len(source) > 20000:
        return JsonResponse({"error": gettext("代码不能超过 20000 字符。")}, status=400)
    if not consume("draft", str(request.user.pk), 60, 60):
        return JsonResponse({"error": gettext("保存过于频繁，请稍后重试。")}, status=429)
    Draft.objects.update_or_create(
        user=request.user, version=problem.current, defaults={"source": source}
    )
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"saved": True})
    messages.success(request, gettext("草稿已私密保存。"))
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
            {"error": gettext("请提供不超过 20000 字符的源代码及有效的提交方式。")},
            status=400,
        )
    if request.POST.get("version") != str(problem.current_id):
        return JsonResponse({"error": gettext("题目版本已更新，请刷新后重试。")}, status=409)
    if not consume("submit", str(request.user.pk), 12, 60):
        return JsonResponse({"error": gettext("提交过于频繁，请稍后再试。")}, status=429)
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
            "public_cases": localized_public_cases(job.version, safe_result(job)),
            "reservation": reservation_data(job),
            "shareable": job.version.data.get("evaluator") not in SPECIALISTS
            and is_formal_evidence(job),
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
            {
                "title": gettext("注册暂未开放"),
                "body": gettext("本站暂不接受新账号。公开知识和题目仍可浏览。"),
            },
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
            "title": gettext("创建学习账号"),
            "button": gettext("注册"),
            "intro": gettext("只需要用户名与密码。代码和学习记录默认仅你可见。"),
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
                from django.utils import timezone

                AccessToken.objects.filter(user=user, revoked_at=None).update(
                    revoked_at=timezone.now()
                )
                code = secrets.token_urlsafe(32)
                profile.recovery_hash = make_password(code)
                profile.save(update_fields=["recovery_hash"])
                login(request, user)
                return render(request, "recovery_code.html", {"code": code})
        form.add_error(None, gettext("用户名或恢复码无效。"))
    return render(
        request,
        "account_form.html",
        {
            "form": form,
            "title": gettext("恢复账号"),
            "button": gettext("重置密码"),
            "intro": gettext("使用注册时保存的恢复码。成功后旧码作废，原登录会话失效。"),
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
        messages.success(request, gettext("个人资料已保存。"))
        return redirect("account")
    return render(request, "account.html", {"form": form})


@require_POST
@login_required
def rotate_recovery(request):
    if not request.user.check_password(request.POST.get("password", "")):
        return HttpResponse(gettext("密码不正确。"), status=400)
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
        "cpu_budget": usage(request.user),
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
                "result": safe_result(j),
                "resource_reservation": reservation_data(j),
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
        "learning": export_learning(request.user),
        "community": export_community(request.user),
        "practice_api": export_practice_api(request.user),
        "showcase": export_showcase(request.user),
        "organizations": export_organizations(request.user),
        "coaching": export_coaching(request.user),
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
        return HttpResponse(gettext("请输入当前密码，并输入 DELETE 确认删除。"), status=400)
    user = request.user
    logout(request)
    user.delete()
    return render(
        request,
        "message.html",
        {
            "title": gettext("账号已删除"),
            "body": gettext(
                "账号、草稿、提交、学习记录及你发布的社区内容已从当前数据库删除。"
                "审核事件只保留不含正文的操作记录，操作者关联已清除。备份按站点保留周期清除。"
            )
            + gettext(
                "组织拥有的课程与题库版本继续保留，作者、审核者和操作者关联会清除；你的成员关系、收到的邀请与课程加入记录会删除。"
            ),
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
