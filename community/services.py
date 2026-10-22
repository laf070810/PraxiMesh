"""Authorization and locked transitions for community writing and private cases."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext

from community.models import Appeal, Comment, ModerationEvent, Post, Report, State

MODERATE_PERMISSION = "community.moderate_content"
REVIEWABLE_STATES = (State.PENDING, State.PUBLISHED, State.REJECTED, State.HIDDEN)


class CommunityError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def is_moderator(user):
    return user.is_authenticated and user.has_perm(MODERATE_PERMISSION)


def require_moderator(user):
    if not is_moderator(user):
        raise PermissionDenied


def public_posts():
    return Post.objects.filter(state=State.PUBLISHED).filter(
        Q(version__isnull=True)
        | Q(version__problem__published=True, version__problem__current__isnull=False)
    )


def post_is_public(post):
    return post.state == State.PUBLISHED and (
        post.version_id is None
        or (post.version.problem.published and post.version.problem.current_id is not None)
    )


def visible_posts(user):
    allowed = Q(pk__in=public_posts().values("pk"))
    if user.is_authenticated:
        allowed |= Q(author=user)
    if is_moderator(user):
        allowed |= Q(state__in=REVIEWABLE_STATES)
    return Post.objects.filter(allowed).select_related("author", "version__problem")


def visible_comments(post, user):
    allowed = Q(author=user) if user.is_authenticated else Q(pk__in=[])
    if post_is_public(post):
        allowed |= Q(state=State.PUBLISHED)
    if is_moderator(user):
        allowed |= Q(state__in=REVIEWABLE_STATES)
    return Comment.objects.filter(post=post).filter(allowed).select_related("author")


def model_for(kind):
    if kind == "post":
        return Post
    if kind == "comment":
        return Comment
    raise Http404


def get_writing(kind, pk, *, lock=False):
    query = model_for(kind).objects
    if lock:
        query = query.select_for_update()
    try:
        return query.get(pk=pk)
    except model_for(kind).DoesNotExist:
        raise Http404 from None


def own_writing(user, kind, pk):
    item = get_writing(kind, pk, lock=True)
    if not user.is_authenticated or item.author_id != user.pk:
        raise Http404
    return item


def expected_revision(item, revision):
    if item.revision != revision:
        raise CommunityError(gettext("内容已改变，请刷新页面后重试。"))


def close_appeals(kind, item):
    Appeal.objects.filter(**{kind: item}, status=Appeal.Status.OPEN).update(
        status=Appeal.Status.SUPERSEDED, updated_at=timezone.now()
    )


def validate_version(post):
    if post.version_id and (
        not post.version.problem.published or post.version.problem.current_id is None
    ):
        raise CommunityError(gettext("关联题目已经下线，不能提交或公开这篇内容。"))


def validate_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(gettext("正文或说明为空，或超过长度限制。"))


@transaction.atomic
def save_post(user, data, *, pk=None, revision=None):
    if not user.is_authenticated:
        raise PermissionDenied
    post = own_writing(user, "post", pk) if pk else Post(author=user)
    if pk:
        expected_revision(post, revision)
    for field in ("kind", "title", "body", "version"):
        setattr(post, field, data[field])
    validate_text(post.title, 160)
    validate_text(post.body, 20000)
    validate_version(post)
    if pk:
        post.revision += 1
        post.state = State.DRAFT if post.state in (State.DRAFT, State.WITHDRAWN) else State.PENDING
        post.moderation_note = ""
        close_appeals("post", post)
    post.full_clean()
    post.save()
    return post


@transaction.atomic
def save_comment(user, body, *, post_id=None, pk=None, revision=None):
    if not user.is_authenticated:
        raise PermissionDenied
    if pk:
        comment = own_writing(user, "comment", pk)
        expected_revision(comment, revision)
        comment.revision += 1
        comment.state = (
            State.DRAFT if comment.state in (State.DRAFT, State.WITHDRAWN) else State.PENDING
        )
        comment.moderation_note = ""
        close_appeals("comment", comment)
    else:
        try:
            post = public_posts().select_for_update(of=("self",)).get(pk=post_id)
        except Post.DoesNotExist:
            raise Http404 from None
        comment = Comment(author=user, post=post)
    comment.body = body
    validate_text(body, 5000)
    comment.full_clean()
    comment.save()
    return comment


@transaction.atomic
def author_action(user, kind, pk, action, revision):
    item = own_writing(user, kind, pk)
    expected_revision(item, revision)
    if action == "delete":
        item.delete()
        return
    if action == "submit":
        if item.state in (State.PENDING, State.PUBLISHED):
            raise CommunityError(gettext("这篇内容已送审或公开。"))
        if kind == "post":
            validate_version(item)
        elif not post_is_public(item.post):
            raise CommunityError(gettext("讨论当前未公开，评论不能送审。"))
        item.state = State.PENDING
        item.moderation_note = ""
    elif action == "withdraw":
        item.state = State.WITHDRAWN
    else:
        raise Http404
    close_appeals(kind, item)
    item.save(update_fields=["state", "moderation_note", "updated_at"])


def event(actor, kind, item, action):
    ModerationEvent.objects.create(
        actor=actor,
        target_kind=kind,
        target_id=item.pk,
        target_revision=getattr(item, "revision", 0),
        action=action,
    )


@transaction.atomic
def moderate(user, kind, pk, action, revision, note):
    require_moderator(user)
    item = get_writing(kind, pk, lock=True)
    if item.author_id == user.pk:
        raise PermissionDenied(gettext("不能审核自己的内容。"))
    expected_revision(item, revision)
    if action in ("publish", "reject") and item.state == State.PENDING:
        if action == "publish":
            if kind == "post":
                validate_version(item)
            elif not post_is_public(item.post):
                raise CommunityError(gettext("请先处理父讨论；未公开讨论的评论不能公开。"))
        item.state = State.PUBLISHED if action == "publish" else State.REJECTED
    elif action == "hide" and item.state == State.PUBLISHED:
        item.state = State.HIDDEN
    else:
        raise CommunityError(gettext("内容状态已经改变，此审核操作不适用。"))
    validate_text(note, 1000)
    item.moderation_note = note.strip()
    item.save(update_fields=["state", "moderation_note", "updated_at"])
    event(user, kind, item, action)
    return item


def public_target(kind, pk):
    item = get_writing(kind, pk, lock=True)
    if kind == "post":
        visible = post_is_public(item)
    else:
        visible = item.state == State.PUBLISHED and post_is_public(item.post)
    if not visible:
        raise Http404
    return item


@transaction.atomic
def create_report(user, kind, pk, reason):
    if not user.is_authenticated:
        raise PermissionDenied
    item = public_target(kind, pk)
    validate_text(reason, 2000)
    if Report.objects.filter(reporter=user, status=Report.Status.OPEN, **{kind: item}).exists():
        raise CommunityError(gettext("你已经提交过这个内容的举报，请等待处理。"))
    report = Report(reporter=user, reason=reason, **{kind: item})
    report.full_clean()
    report.save()
    return report


@transaction.atomic
def create_appeal(user, kind, pk, reason, revision):
    item = own_writing(user, kind, pk)
    expected_revision(item, revision)
    validate_text(reason, 2000)
    if item.state not in (State.REJECTED, State.HIDDEN):
        raise CommunityError(gettext("只有未通过审核或被隐藏的内容可以申诉。"))
    if Appeal.objects.filter(author=user, target_revision=item.revision, **{kind: item}).exists():
        raise CommunityError(gettext("这个版本已经提交过申诉；处理结果可在我的申诉中查看。"))
    appeal = Appeal(author=user, target_revision=item.revision, reason=reason, **{kind: item})
    appeal.full_clean()
    appeal.save()
    return appeal


@transaction.atomic
def resolve_report(user, pk, decision, note):
    require_moderator(user)
    validate_text(note, 1000)
    try:
        report = Report.objects.select_for_update().get(pk=pk)
    except Report.DoesNotExist:
        raise Http404 from None
    target = report.post or report.comment
    if report.reporter_id == user.pk or target.author_id == user.pk:
        raise PermissionDenied(gettext("不能处理自己发起或涉及自己内容的举报。"))
    if report.status != Report.Status.OPEN:
        raise CommunityError(gettext("这条举报已经处理。"))
    if decision not in (Report.Status.RESOLVED, Report.Status.DISMISSED):
        raise CommunityError(gettext("无效的举报决定。"), 400)
    report.status, report.resolution, report.resolved_by = decision, note, user
    report.full_clean()
    report.save()
    event(user, "report", report, decision)
    return report


@transaction.atomic
def resolve_appeal(user, pk, decision, note):
    require_moderator(user)
    validate_text(note, 1000)
    try:
        initial = Appeal.objects.get(pk=pk)
    except Appeal.DoesNotExist:
        raise Http404 from None
    # Editing also locks the writing before superseding its appeals; keep that order.
    kind = "post" if initial.post_id else "comment"
    target = get_writing(kind, initial.post_id or initial.comment_id, lock=True)
    try:
        appeal = Appeal.objects.select_for_update().get(pk=pk)
    except Appeal.DoesNotExist:
        raise Http404 from None
    if appeal.author_id == user.pk:
        raise PermissionDenied(gettext("不能处理自己的申诉。"))
    if appeal.status != Appeal.Status.OPEN:
        raise CommunityError(gettext("这条申诉已经处理。"))
    if target.revision != appeal.target_revision or target.state not in (
        State.REJECTED,
        State.HIDDEN,
    ):
        raise CommunityError(gettext("被申诉内容已经改变，请刷新页面。"))
    if decision == Appeal.Status.REOPENED:
        target.state = State.PENDING
        target.moderation_note = gettext("申诉已接受，等待重新审核。")
        target.save(update_fields=["state", "moderation_note", "updated_at"])
    elif decision != Appeal.Status.UPHELD:
        raise CommunityError(gettext("无效的申诉决定。"), 400)
    appeal.status, appeal.resolution, appeal.resolved_by = decision, note, user
    appeal.full_clean()
    appeal.save()
    event(user, "appeal", appeal, decision)
    return appeal


def export_user_data(user):
    """Own writing/case text only; no reported author's content or private submissions."""

    def rows(query, fields):
        result = []
        for row in query.values(*fields):
            result.append(
                {
                    key: value.isoformat()
                    if hasattr(value, "isoformat")
                    else str(value)
                    if key.endswith("id") and value is not None
                    else value
                    for key, value in row.items()
                }
            )
        return result

    writing = ["id", "body", "state", "revision", "moderation_note", "created_at", "updated_at"]
    cases = [
        "id",
        "post_id",
        "comment_id",
        "reason",
        "status",
        "resolution",
        "created_at",
        "updated_at",
    ]
    return {
        "posts": rows(Post.objects.filter(author=user), [*writing, "title", "kind", "version_id"]),
        "comments": rows(Comment.objects.filter(author=user), [*writing, "post_id"]),
        "reports": rows(Report.objects.filter(reporter=user), cases),
        "appeals": rows(Appeal.objects.filter(author=user), [*cases, "target_revision"]),
        "moderation_events": rows(
            ModerationEvent.objects.filter(actor=user),
            ["id", "target_kind", "target_id", "target_revision", "action", "created_at"],
        ),
    }
