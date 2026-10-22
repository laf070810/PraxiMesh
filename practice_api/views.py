import json
import uuid
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.content import public_problem
from core.limits import consume
from core.middleware import client_address
from core.models import Problem, Submission
from core.queue import AdmissionError
from judge.backends import result_environment_label
from practice_api.forms import TokenForm
from practice_api.models import AccessToken
from practice_api.services import authenticate, issue_token, submit_once


def error(code, status):
    response = JsonResponse({"error": code}, status=status)
    response["Cache-Control"] = "no-store"
    if status == 401:
        response["WWW-Authenticate"] = 'Bearer realm="practice"'
    return response


def bearer(scope):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not consume("practice-api-ip", client_address(request), 120, 60):
                return error("rate_limit", 429)
            authorization = request.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                return error("invalid_token", 401)
            token = authenticate(authorization[7:])
            if token is None:
                return error("invalid_token", 401)
            if not getattr(token, scope):
                return error("insufficient_scope", 403)
            if not consume("practice-api", str(token.user_id), 60, 60):
                return error("rate_limit", 429)
            request.api_user = token.user
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


@require_GET
def problem_list(request):
    rows = Problem.objects.filter(published=True, current__isnull=False).select_related("current")
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return JsonResponse(
        {
            "format": 1,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "problems": [
                {
                    "id": row.pk,
                    "version_id": row.current_id,
                    "revision": row.current.revision,
                    "title": row.current.data["title"],
                    "language": row.current.data["language"],
                }
                for row in page
            ],
        }
    )


@require_GET
def problem_package(request, slug):
    problem = get_object_or_404(Problem.objects.select_related("current"), pk=slug, published=True)
    if not problem.current_id:
        return error("not_found", 404)
    version = problem.current
    data = public_problem(version)
    return JsonResponse(
        {
            "format": 1,
            "version_id": version.pk,
            "source_digest": version.digest,
            "problem": data,
            "public_tests": [
                {
                    "name": row["name"],
                    **({"stdin": row["stdin"]} if "stdin" in row else {"args": row["args"]}),
                    "expected": row["expected"],
                }
                for row in version.data["tests"]
                if row["public"]
            ],
            "formal_result": False,
        }
    )


@csrf_exempt
@require_POST
@bearer("can_submit")
def submit(request, slug):
    # This endpoint accepts bearer credentials only; a logged-in browser session
    # alone never bypasses CSRF on ordinary submission or account endpoints.
    if request.content_type != "application/json" or len(request.body) > 100000:
        return error("invalid_request", 400)
    try:

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate key")
                result[key] = value
            return result

        payload = json.loads(request.body.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(payload, dict) or set(payload) != {"version_id", "source", "mode"}:
            raise ValueError("Invalid fields")
        version = payload["version_id"]
        source, mode = payload["source"], payload["mode"]
        if type(version) is not int or version < 1 or not isinstance(source, str):
            raise ValueError("Invalid types")
        if not source.strip() or len(source) > 20000 or mode not in ("run", "submit"):
            raise ValueError("Invalid source or mode")
        source.encode("utf-8")
        key = uuid.UUID(request.headers.get("Idempotency-Key", ""))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return error("invalid_request", 400)
    try:
        job, created = submit_once(request.api_user, key, slug, version, source, mode)
    except AdmissionError as exc:
        return error(str(exc), exc.status)
    return JsonResponse(
        {
            "id": str(job.pk),
            "created": created,
            "status": job.status,
            "url": f"/api/v1/submissions/{job.pk}/",
        },
        status=202,
    )


@require_GET
@bearer("can_read_results")
def result(request, job_id):
    job = get_object_or_404(
        Submission.objects.select_related("version"), pk=job_id, user=request.api_user
    )
    return JsonResponse(
        {
            "id": str(job.pk),
            "problem": job.version.problem_id,
            "version_id": job.version_id,
            "revision": job.version.revision,
            "source_digest": job.version.digest,
            "mode": job.mode,
            "status": job.status,
            "verdict": job.verdict,
            "result": job.result,
            "evaluation_fingerprint": job.evaluation_fingerprint,
            "evaluation_environment": result_environment_label(job.environment),
            "formal_result": job.mode == "submit"
            and job.status == "finished"
            and job.verdict
            in {
                "accepted",
                "wrong_answer",
                "syntax_error",
                "runtime_error",
                "time_limit",
                "memory_limit",
                "output_limit",
            },
        }
    )


@require_http_methods(["GET", "POST"])
@login_required
def tokens(request):
    form = TokenForm(request.POST or None)
    raw = None
    if request.method == "POST" and form.is_valid():
        if not request.user.check_password(form.cleaned_data["password"]):
            form.add_error("password", _("密码不正确。"))
        else:
            try:
                token, raw = issue_token(
                    request.user,
                    form.cleaned_data["label"],
                    form.cleaned_data["days"],
                    can_submit=form.cleaned_data["can_submit"],
                    can_read_results=form.cleaned_data["can_read_results"],
                )
                form = TokenForm()
            except ValueError:
                form.add_error(None, _("无法创建令牌，请检查有效期、权限和十个有效令牌的上限。"))
    page = Paginator(AccessToken.objects.filter(user=request.user), 20).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "practice_api/tokens.html",
        {"form": form, "raw_token": raw, "page": page, "now": timezone.now()},
    )


@require_POST
@login_required
def revoke(request, token_id):
    token = get_object_or_404(AccessToken, pk=token_id, user=request.user)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return redirect("practice_api:tokens")
