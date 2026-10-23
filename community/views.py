from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from community import forms, services
from community.models import Appeal, Comment, Post, Report, State
from core.limits import consume
from core.models import Problem


def guarded(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST" and not consume(
            "community-write", str(request.user.pk), 30, 60
        ):
            return HttpResponse(gettext("操作过于频繁，请稍后重试。"), status=429)
        try:
            return view(request, *args, **kwargs)
        except services.CommunityError as error:
            return render(
                request, "community/error.html", {"error": str(error)}, status=error.status
            )
        except ValidationError:
            return render(
                request,
                "community/error.html",
                {"error": gettext("内容格式或长度不符合要求。")},
                status=400,
            )

    return wrapped


def writing_url(kind, item):
    return reverse(
        "community:detail" if kind == "post" else "community:comment_detail", args=[item.pk]
    )


def writing_for_reader(user, kind, pk):
    if kind == "post":
        return get_object_or_404(services.visible_posts(user), pk=pk)
    if kind != "comment":
        raise Http404
    item = get_object_or_404(
        Comment.objects.select_related("post__version__problem", "author"), pk=pk
    )
    # Moderators can review submitted comment text even when its parent is
    # withdrawn. This does not grant access to the parent's title or body.
    post = services.visible_posts(user).filter(pk=item.post_id).first()
    if post is None:
        if services.is_moderator(user) and item.state in services.REVIEWABLE_STATES:
            return item
        raise Http404
    return get_object_or_404(services.visible_comments(post, user), pk=pk)


@require_GET
def index(request):
    posts = services.public_posts().select_related("author", "version__problem")
    kind = request.GET.get("kind", "")
    if kind in Post.Kind.values:
        posts = posts.filter(kind=kind)
    elif kind:
        raise Http404
    problem = request.GET.get("problem", "")
    if problem:
        target = get_object_or_404(Problem, pk=problem, published=True, current__isnull=False)
        posts = posts.filter(version__problem=target)
    return render(
        request,
        "community/index.html",
        {
            "page": Paginator(posts, 20).get_page(request.GET.get("page")),
            "kind": kind,
            "problem": problem,
        },
    )


def blog_directory(request, username=None):
    # A public directory is not an account lookup. Private/withdrawn blogs and
    # accounts without public blog posts do not create a discoverable directory.
    posts = services.public_posts().filter(kind=Post.Kind.BLOG).select_related("author")
    if username is not None:
        posts = posts.filter(author__username=username)
        if not posts.exists():
            raise Http404
    return render(
        request,
        "community/blogs.html",
        {
            "page": Paginator(posts, 20).get_page(request.GET.get("page")),
            "blog_author": username,
        },
    )


@require_GET
def blogs(request):
    return blog_directory(request)


@require_GET
def author_blog(request, username):
    return blog_directory(request, username)


@require_GET
@login_required
def mine(request):
    kind = "comment" if request.GET.get("kind") == "comment" else "post"
    items = services.model_for(kind).objects.filter(author=request.user)
    return render(
        request,
        "community/mine.html",
        {"page": Paginator(items, 20).get_page(request.GET.get("page")), "kind": kind},
    )


def detail_context(request, kind, item):
    own = request.user.is_authenticated and item.author_id == request.user.pk
    moderator = services.is_moderator(request.user)
    return {
        "item": item,
        "kind": kind,
        "own": own,
        "moderator": moderator,
        "is_public": services.post_is_public(item if kind == "post" else item.post)
        and item.state == State.PUBLISHED,
        "can_submit": own and item.state not in (State.PENDING, State.PUBLISHED),
        "can_appeal": own and item.state in (State.REJECTED, State.HIDDEN),
        "can_moderate": moderator and not own and item.state in (State.PENDING, State.PUBLISHED),
        "moderation_form": forms.ModerationForm(initial={"revision": item.revision}),
    }


@require_GET
def detail(request, pk):
    post = writing_for_reader(request.user, "post", pk)
    context = detail_context(request, "post", post)
    context["comments"] = Paginator(services.visible_comments(post, request.user), 20).get_page(
        request.GET.get("page")
    )
    return render(request, "community/detail.html", context)


@require_GET
def comment_detail(request, pk):
    comment = writing_for_reader(request.user, "comment", pk)
    context = detail_context(request, "comment", comment)
    context["parent_visible"] = (
        services.visible_posts(request.user).filter(pk=comment.post_id).exists()
    )
    return render(request, "community/comment.html", context)


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def post_edit(request, pk=None):
    post = get_object_or_404(Post, pk=pk, author=request.user) if pk else None
    initial = (
        {key: getattr(post, key) for key in ("kind", "title", "body", "version", "revision")}
        if post
        else {}
    )
    form = forms.PostForm(request.POST if request.method == "POST" else None, initial=initial)
    if post is None and request.method == "GET":
        for field in ("kind", "version"):
            if request.GET.get(field):
                form.initial[field] = form.fields[field].clean(request.GET[field])
    if request.method == "POST" and form.is_valid():
        post = services.save_post(
            request.user, form.cleaned_data, pk=pk, revision=form.cleaned_data.get("revision")
        )
        messages.success(
            request, gettext("内容已保存。私有草稿需主动送审；已送审内容的编辑会重新等待审核。")
        )
        return redirect("community:detail", pk=post.pk)
    return render(
        request,
        "community/edit.html",
        {
            "form": form,
            "heading": gettext("编辑内容") if post else gettext("写讨论、提问、题解或博文"),
        },
        status=400 if request.method == "POST" else 200,
    )


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def comment_edit(request, pk=None, post_id=None):
    comment = get_object_or_404(Comment, pk=pk, author=request.user) if pk else None
    if not comment:
        get_object_or_404(services.public_posts(), pk=post_id)
    initial = {"body": comment.body, "revision": comment.revision} if comment else {}
    form = forms.CommentForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        comment = services.save_comment(
            request.user,
            form.cleaned_data["body"],
            post_id=post_id,
            pk=pk,
            revision=form.cleaned_data.get("revision"),
        )
        messages.success(request, gettext("评论已保存，需主动送审后才可能公开。"))
        if services.visible_posts(request.user).filter(pk=comment.post_id).exists():
            return redirect("community:comment_detail", pk=comment.pk)
        return redirect(reverse("community:mine") + "?kind=comment")
    return render(
        request,
        "community/edit.html",
        {"form": form, "heading": gettext("编辑评论")},
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@login_required
@guarded
def author_action(request, kind, pk, action):
    form = forms.RevisionForm(request.POST)
    if not form.is_valid():
        return HttpResponse(gettext("请刷新页面后重试。"), status=400)
    services.author_action(request.user, kind, pk, action, form.cleaned_data["revision"])
    messages.success(
        request,
        {
            "submit": gettext("已提交审核，尚未公开。"),
            "withdraw": gettext("已撤回，不再公开。"),
            "delete": gettext("已删除正文及关联评论和私有处理记录。"),
        }.get(action, gettext("操作完成。")),
    )
    return redirect("community:mine")


@require_POST
@login_required
@guarded
def moderate(request, kind, pk):
    services.require_moderator(request.user)
    form = forms.ModerationForm(request.POST)
    if not form.is_valid():
        return HttpResponse(gettext("请填写有效的审核决定和处理说明。"), status=400)
    item = services.moderate(
        request.user,
        kind,
        pk,
        form.cleaned_data["action"],
        form.cleaned_data["revision"],
        form.cleaned_data["note"],
    )
    messages.success(request, gettext("审核决定已保存。"))
    return redirect(writing_url(kind, item))


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def report_create(request, kind, pk):
    target = writing_for_reader(request.user, kind, pk)
    if target.state != State.PUBLISHED or not services.post_is_public(
        target if kind == "post" else target.post
    ):
        raise Http404
    form = forms.ReasonForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        case = services.create_report(request.user, kind, pk, form.cleaned_data["reason"])
        return redirect("community:report_detail", pk=case.pk)
    return render(
        request,
        "community/edit.html",
        {"form": form, "heading": gettext("私有举报"), "private_case": True},
        status=400 if request.method == "POST" else 200,
    )


@require_http_methods(["GET", "POST"])
@login_required
@guarded
def appeal_create(request, kind, pk):
    target = get_object_or_404(services.model_for(kind), pk=pk, author=request.user)
    if target.state not in (State.REJECTED, State.HIDDEN):
        raise Http404
    form = forms.ReasonForm(
        request.POST if request.method == "POST" else None, initial={"revision": target.revision}
    )
    if request.method == "POST" and form.is_valid():
        case = services.create_appeal(
            request.user, kind, pk, form.cleaned_data["reason"], form.cleaned_data.get("revision")
        )
        return redirect("community:appeal_detail", pk=case.pk)
    return render(
        request,
        "community/edit.html",
        {"form": form, "heading": gettext("私有申诉"), "private_case": True},
        status=400 if request.method == "POST" else 200,
    )


def private_cases(request, kind):
    model = Report if kind == "report" else Appeal
    field = "reporter" if kind == "report" else "author"
    query = model.objects.filter(**{field: request.user})
    return render(
        request,
        "community/cases.html",
        {"kind": kind, "page": Paginator(query, 20).get_page(request.GET.get("page"))},
    )


@require_GET
@login_required
def reports(request):
    return private_cases(request, "report")


@require_GET
@login_required
def appeals(request):
    return private_cases(request, "appeal")


def case_detail(request, kind, pk):
    model = Report if kind == "report" else Appeal
    query = model.objects.all()
    if not services.is_moderator(request.user):
        query = query.filter(**{"reporter" if kind == "report" else "author": request.user})
    case = get_object_or_404(query, pk=pk)
    form = forms.ReportDecisionForm() if kind == "report" else forms.AppealDecisionForm()
    return render(
        request,
        "community/case.html",
        {
            "case": case,
            "kind": kind,
            "decision_form": form,
            "moderator": services.is_moderator(request.user),
            "target_kind": "post" if case.post_id else "comment",
            "target_id": case.post_id or case.comment_id,
        },
    )


@require_GET
@login_required
def report_detail(request, pk):
    return case_detail(request, "report", pk)


@require_GET
@login_required
def appeal_detail(request, pk):
    return case_detail(request, "appeal", pk)


@require_POST
@login_required
@guarded
def resolve_case(request, kind, pk):
    services.require_moderator(request.user)
    if kind not in ("report", "appeal"):
        raise Http404
    form_type = forms.ReportDecisionForm if kind == "report" else forms.AppealDecisionForm
    form = form_type(request.POST)
    if not form.is_valid():
        return HttpResponse(gettext("请填写有效的处理决定和说明。"), status=400)
    resolve = services.resolve_report if kind == "report" else services.resolve_appeal
    resolve(request.user, pk, form.cleaned_data["decision"], form.cleaned_data["note"])
    messages.success(request, gettext("处理结果已保存，仅本人及审核者可以查看。"))
    return redirect("community:" + kind + "_detail", pk=pk)


@require_GET
@login_required
def moderation(request):
    services.require_moderator(request.user)
    kind = request.GET.get("kind", "post")
    queries = {
        "post": Post.objects.filter(state=State.PENDING),
        "comment": Comment.objects.filter(state=State.PENDING),
        "report": Report.objects.filter(status=Report.Status.OPEN),
        "appeal": Appeal.objects.filter(status=Appeal.Status.OPEN),
    }
    if kind not in queries:
        raise Http404
    return render(
        request,
        "community/moderation.html",
        {"kind": kind, "page": Paginator(queries[kind], 20).get_page(request.GET.get("page"))},
    )
