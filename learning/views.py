from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.content import digest
from core.limits import consume
from core.models import KnowledgeNode, Problem
from learning.evidence import build_evidence
from learning.forms import NoteForm
from learning.models import Bookmark, Note, TutorialReading


def limit_writes(view):
    @wraps(view)
    def limited(request, *args, **kwargs):
        if request.method == "POST" and not consume("learning-write", str(request.user.pk), 60, 60):
            response = HttpResponse(gettext("保存操作过于频繁，请一分钟后重试。"), status=429)
            response["Retry-After"] = "60"
            return response
        return view(request, *args, **kwargs)

    return limited


@require_GET
@login_required
def index(request):
    evidence = build_evidence(request.user)
    evidence["recommendations"] = Paginator(evidence["recommendations"], 10).get_page(
        request.GET.get("page")
    )
    return render(request, "learning/index.html", evidence)


@require_GET
@login_required
def bookmarks(request):
    page = Paginator(
        Bookmark.objects.filter(user=request.user).select_related("problem__current"), 20
    ).get_page(request.GET.get("page"))
    return render(request, "learning/bookmarks.html", {"page": page})


@require_POST
@login_required
@limit_writes
def bookmark(request, slug):
    action = request.POST.get("action")
    if action == "save":
        problem = get_object_or_404(Problem, pk=slug, published=True, current__isnull=False)
        Bookmark.objects.get_or_create(user=request.user, problem=problem)
        messages.success(request, gettext("题目已加入私有收藏。"))
    elif action == "remove":
        Bookmark.objects.filter(user=request.user, problem_id=slug).delete()
        messages.success(request, gettext("已取消收藏。"))
    else:
        return HttpResponse(gettext("请选择收藏或取消收藏。"), status=400)
    return redirect("learning:bookmarks")


@require_GET
@login_required
def notes(request):
    page = Paginator(
        Note.objects.filter(user=request.user).select_related("version", "node"), 20
    ).get_page(request.GET.get("page"))
    return render(request, "learning/notes.html", {"page": page})


@require_http_methods(["GET", "POST"])
@login_required
@limit_writes
def note_create(request):
    form = NoteForm(
        request.POST if request.method == "POST" else None,
        initial={"version": request.GET.get("version"), "node": request.GET.get("node")},
    )
    if request.method == "POST" and form.is_valid():
        note = form.save(commit=False)
        note.user = request.user
        note.save()
        return redirect("learning:note_detail", pk=note.pk)
    return render(
        request,
        "learning/note_form.html",
        {"form": form, "editing": False},
        status=400 if request.method == "POST" else 200,
    )


@require_GET
@login_required
def note_detail(request, pk):
    note = get_object_or_404(
        Note.objects.select_related("version__problem", "node"), pk=pk, user=request.user
    )
    return render(request, "learning/note.html", {"note": note})


@require_http_methods(["GET", "POST"])
@login_required
@limit_writes
def note_edit(request, pk):
    note = get_object_or_404(Note, pk=pk, user=request.user)
    form = NoteForm(request.POST if request.method == "POST" else None, instance=note)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("learning:note_detail", pk=note.pk)
    return render(
        request,
        "learning/note_form.html",
        {"form": form, "editing": True, "note": note},
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@login_required
@limit_writes
def note_delete(request, pk):
    get_object_or_404(Note, pk=pk, user=request.user).delete()
    messages.success(request, gettext("笔记已删除。"))
    return redirect("learning:notes")


@require_GET
@login_required
def reading(request):
    declarations = {
        item.tutorial_id: item for item in TutorialReading.objects.filter(user=request.user)
    }
    tutorials = KnowledgeNode.objects.filter(
        Q(kind="tutorial", published=True) | Q(pk__in=declarations)
    )
    page = Paginator(tutorials, 20).get_page(request.GET.get("page"))
    rows = []
    for tutorial in page:
        declaration = declarations.get(tutorial.pk)
        rows.append(
            {
                "tutorial": tutorial,
                "reading": declaration,
                "changed": bool(
                    declaration and declaration.content_digest != digest(tutorial.data)
                ),
            }
        )
    return render(request, "learning/reading.html", {"page": page, "rows": rows})


@require_POST
@login_required
@limit_writes
def reading_mark(request, slug):
    action = request.POST.get("action")
    if action == "complete":
        tutorial = get_object_or_404(KnowledgeNode, pk=slug, kind="tutorial", published=True)
        TutorialReading.objects.update_or_create(
            user=request.user,
            tutorial=tutorial,
            defaults={"completed_at": timezone.now(), "content_digest": digest(tutorial.data)},
        )
        messages.success(request, gettext("已记录你自述的阅读完成状态。"))
    elif action == "reset":
        TutorialReading.objects.filter(user=request.user, tutorial_id=slug).delete()
        messages.success(request, gettext("已取消阅读标记。"))
    else:
        return HttpResponse(gettext("请选择阅读完成或取消标记。"), status=400)
    return redirect("learning:reading")
