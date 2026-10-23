from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.limits import consume
from core.models import Problem
from localization.presentation import localized_problem
from organizations import forms, packages, services
from organizations.models import (
    Course,
    CourseEnrollment,
    Invitation,
    Membership,
    Package,
    PackageRevision,
    Role,
)
from organizations.progress import own_progress


def guarded(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST" and not consume(
            "organizations-write", str(request.user.pk), 30, 60
        ):
            return HttpResponse(_("操作过于频繁，请稍后重试。"), status=429)
        try:
            return view(request, *args, **kwargs)
        except (services.OrganizationError, ValidationError):
            return render(request, "organizations/error.html", status=409)

    return wrapped


def page(request, query):
    return Paginator(query, 20).get_page(request.GET.get("page"))


@require_GET
@login_required
def index(request):
    return render(
        request,
        "organizations/index.html",
        {
            "page": page(
                request, Membership.objects.filter(user=request.user).select_related("organization")
            ),
            "enrollments": page(
                request,
                CourseEnrollment.objects.filter(user=request.user)
                .select_related("revision__course")
                .order_by("-created_at", "-pk"),
            ),
            "invitations": page(
                request,
                Invitation.objects.filter(
                    target=request.user, state=Invitation.State.PENDING
                ).select_related("organization"),
            ),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def create(request):
    form = forms.OrganizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        org = services.create_organization(request.user, **form.cleaned_data)
        return redirect("organizations:detail", org_id=org.pk)
    return render(
        request,
        "organizations/form.html",
        {"form": form, "heading": _("创建私有组织")},
        status=400 if request.method == "POST" else 200,
    )


@require_GET
@login_required
def detail(request, org_id):
    org, member = services.require_role(request.user, org_id)
    courses = org.courses.select_related("current")
    if member.role not in services.EDITORS:
        courses = courses.filter(published=True)
    return render(
        request,
        "organizations/detail.html",
        {
            "org": org,
            "member": member,
            "can_edit": member.role in services.EDITORS,
            "courses": page(request, courses),
            "packages": page(request, org.packages.select_related("current").all())
            if member.role in services.EDITORS
            else None,
            "members": page(request, org.memberships.select_related("user"))
            if member.role == Role.OWNER
            else None,
            "invitation_form": forms.InvitationForm(),
            "role_form": forms.RoleForm(),
        },
    )


@require_POST
@login_required
@guarded
def invite(request, org_id):
    form = forms.InvitationForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("请填写有效的用户名和角色。"), status=400)
    services.invite(request.user, org_id, **form.cleaned_data)
    messages.success(request, _("邀请已发送，目标账号需要自行接受。"))
    return redirect("organizations:detail", org_id=org_id)


@require_POST
@login_required
@guarded
def invitation_action(request, pk, action):
    services.invitation_action(request.user, pk, action)
    return redirect("organizations:index")


@require_POST
@login_required
@guarded
def member_action(request, org_id, pk, action):
    if action == "remove":
        role = None
    elif action == "role":
        form = forms.RoleForm(request.POST)
        if not form.is_valid():
            return HttpResponse(_("请填写有效的组织角色。"), status=400)
        role = form.cleaned_data["role"]
    else:
        raise Http404
    services.change_member(request.user, org_id, pk, role)
    return redirect("organizations:index")


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def course_create(request, org_id):
    org, member = services.require_role(request.user, org_id, services.EDITORS)
    first = (
        Problem.objects.filter(published=True, current__isnull=False)
        .select_related("current")
        .order_by("pk")
        .first()
    )
    initial = {}
    if first:
        source = localized_problem(first.current)
        initial["plan"] = forms.pretty(
            {
                "title": _("组织课程"),
                "summary": source["summary"],
                "units": [
                    {
                        "key": "first",
                        "title": source["title"],
                        "description": source["summary"],
                        "problem": first.pk,
                        "revision": first.current.revision,
                        "digest": first.current.digest,
                    }
                ],
            }
        )
    form = forms.CourseForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        revision = services.save_course(
            request.user, org_id, form.cleaned_data["slug"], form.cleaned_data["plan"]
        )
        return redirect(
            "organizations:course_revision",
            org_id=org_id,
            pk=revision.course_id,
            number=revision.revision,
        )
    return render(
        request,
        "organizations/form.html",
        {"org": org, "form": form, "heading": _("保存新的课程修订")},
        status=400 if request.method == "POST" else 200,
    )


def course_for_reader(request, org_id, pk, number=None):
    org, member = services.require_role(request.user, org_id)
    course = get_object_or_404(Course.objects.select_related("current"), pk=pk, organization=org)
    editor = member.role in services.EDITORS
    if not editor and not course.published:
        raise Http404
    if number is None:
        revision = course.current or (course.revisions.first() if editor else None)
        if revision is None:
            raise Http404
    else:
        if number >= 2**31:
            raise Http404
        revision = get_object_or_404(course.revisions, revision=number)
    if not editor and revision.published_at is None:
        raise Http404
    try:
        services.verify_course_snapshot(revision)
    except services.OrganizationError:
        raise Http404 from None
    return org, course, revision, editor


@require_GET
@login_required
def course(request, org_id, pk, number=None):
    org, course, revision, editor = course_for_reader(request, org_id, pk, number)
    joined = CourseEnrollment.objects.filter(user=request.user, revision=revision).exists()
    cards = own_progress(revision, request.user, include_evidence=joined)
    return render(
        request,
        "organizations/course.html",
        {
            "org": org,
            "course": course,
            "revision": revision,
            "editor": editor,
            "joined": joined,
            "history": page(
                request,
                course.revisions.all()
                if editor
                else course.revisions.filter(published_at__isnull=False),
            ),
            "units": cards,
            "completed": sum(bool(item["evidence"]) for item in cards),
            "digest_form": forms.DigestForm(initial={"digest": revision.digest}),
            "plan": forms.pretty(revision.data) if editor else None,
        },
    )


@require_GET
@login_required
def course_unit(request, org_id, pk, number, key):
    org, course, revision, editor = course_for_reader(request, org_id, pk, number)
    unit = next(
        (
            item
            for item in own_progress(revision, request.user, include_evidence=False)
            if item["key"] == key
        ),
        None,
    )
    if unit is None or not unit["available"]:
        raise Http404
    return render(
        request,
        "organizations/unit.html",
        {"org": org, "course": course, "revision": revision, "unit": unit},
    )


@require_POST
@login_required
@guarded
def course_action(request, org_id, pk, action):
    form = forms.DigestForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("请刷新后核对课程版本。"), status=400)
    services.course_action(request.user, org_id, pk, form.cleaned_data["digest"], action)
    return redirect("organizations:detail", org_id=org_id)


@require_POST
@login_required
@guarded
def enrollment(request, org_id, pk, action):
    services.enroll(request.user, org_id, pk, action)
    return redirect("organizations:detail", org_id=org_id)


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def package_create(request, org_id):
    org, member = services.require_role(request.user, org_id, services.EDITORS)
    form = forms.PackageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        revision = packages.save_package(
            request.user, org_id, form.cleaned_data["slug"], form.cleaned_data["payload"]
        )
        return redirect(
            "organizations:package_revision",
            org_id=org_id,
            pk=revision.package_id,
            number=revision.revision,
        )
    return render(
        request,
        "organizations/form.html",
        {"org": org, "form": form, "heading": _("提交新的题库包修订")},
        status=400 if request.method == "POST" else 200,
    )


@require_GET
@login_required
def package_revision(request, org_id, pk, number):
    org, member = services.require_role(request.user, org_id, services.EDITORS)
    if number >= 2**31:
        raise Http404
    revision = get_object_or_404(
        PackageRevision.objects.select_related("package"),
        package_id=pk,
        package__organization=org,
        revision=number,
    )
    return render(
        request,
        "organizations/package_revision.html",
        {
            "org": org,
            "revision": revision,
            "payload": forms.pretty(revision.payload),
            "history": page(request, revision.package.revisions.all()),
            "digest_form": forms.DigestForm(initial={"digest": revision.digest}),
        },
    )


@require_POST
@login_required
@guarded
def package_submit(request, org_id, pk):
    form = forms.DigestForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("请刷新后核对题库包摘要。"), status=400)
    packages.submit_package(request.user, org_id, pk, form.cleaned_data["digest"])
    return redirect("organizations:detail", org_id=org_id)


@require_POST
@login_required
@guarded
def package_withdraw(request, org_id, pk):
    packages.request_withdrawal(request.user, org_id, pk)
    messages.success(request, _("撤回请求已记录。可信维护命令执行前，既有题目仍可访问。"))
    return redirect("organizations:detail", org_id=org_id)


@require_GET
@login_required
def review_queue(request):
    if not packages.is_reviewer(request.user):
        raise PermissionDenied
    rows = (
        PackageRevision.objects.filter(state=PackageRevision.State.PENDING)
        .exclude(package__organization__memberships__user=request.user)
        .select_related("package__organization")
    )
    return render(request, "organizations/review_queue.html", {"page": page(request, rows)})


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def review(request, pk):
    if not packages.is_reviewer(request.user):
        raise PermissionDenied
    revision = get_object_or_404(
        PackageRevision.objects.select_related("package__organization"), pk=pk
    )
    packages.require_reviewer(request.user, revision.package)
    if revision.state == PackageRevision.State.DRAFT:
        raise Http404
    form = forms.ReviewForm(request.POST or None, initial={"digest": revision.digest})
    if request.method == "POST" and form.is_valid():
        packages.review_package(
            request.user,
            pk,
            form.cleaned_data["digest"],
            form.cleaned_data["action"],
            form.cleaned_data["note"],
        )
        return redirect("organizations:review_queue")
    return render(
        request,
        "organizations/review.html",
        {"revision": revision, "form": form, "payload": forms.pretty(revision.payload)},
        status=400 if request.method == "POST" else 200,
    )


def public_packages():
    return Package.objects.filter(
        state__in=(Package.State.PUBLISHED, Package.State.WITHDRAWAL_REQUESTED),
        current__isnull=False,
    ).select_related("organization", "current")


def public_metadata(package):
    return {
        "id": package.pk,
        "namespace": package.organization_id,
        "slug": package.slug,
        "revision": package.current.revision,
        "digest": package.current.digest,
    }


@require_GET
def published_packages(request):
    result = page(request, public_packages())
    return render(
        request,
        "organizations/public_packages.html",
        {"page": result, "packages": [public_metadata(item) for item in result]},
    )


@require_GET
def public_package(request, pk):
    package = get_object_or_404(public_packages(), pk=pk)
    payload = package.current.payload
    if (
        packages.package_digest(package, payload) != package.current.digest
        or package.current.approved_digest != package.current.digest
    ):
        raise Http404
    objects = (
        package.managed_objects.filter(
            kind="problem",
            active=True,
            problem_version__problem__published=True,
            problem_version__problem__current__isnull=False,
        )
        .select_related("problem_version__problem")
        .order_by("id")
    )
    result = page(request, objects)
    problems = []
    for item in result:
        problem = localized_problem(item.problem_version)
        problems.append(
            {
                "id": item.pk,
                "title": problem["title"],
                "revision": item.problem_version.revision,
                "translation_fallback": problem["translation_fallback"],
            }
        )
    return render(
        request,
        "organizations/public_package.html",
        {
            "package": public_metadata(package),
            "source_url": payload["source_url"],
            "license": payload["license"],
            "attribution": payload["attribution"],
            "problems": problems,
            "page": result,
        },
    )


@require_POST
@login_required
@guarded
def package_cancel(request, org_id, pk):
    form = forms.DigestForm(request.POST)
    if not form.is_valid():
        return HttpResponse(_("请刷新后核对题库包摘要。"), status=400)
    packages.cancel_submission(request.user, org_id, pk, form.cleaned_data["digest"])
    return redirect("organizations:detail", org_id=org_id)
