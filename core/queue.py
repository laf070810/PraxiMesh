import hashlib
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import Problem, QueueLock, Submission, WorkerHealth
from judge.results import LABELS


class AdmissionError(ValueError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def configured_fingerprint():
    # Admission and worker agree on backend, artifact identity and policy.
    from judge.sandbox import policy_digest

    artifact = (
        settings.JUDGE_WASI_SHA256 if settings.JUDGE_BACKEND == "wasi" else settings.JUDGE_IMAGE
    )
    return hashlib.sha256(
        f"{settings.JUDGE_BACKEND}:{artifact}:{policy_digest()}".encode()
    ).hexdigest()


def worker_available():
    return (
        settings.JUDGE_ENABLED
        and WorkerHealth.objects.filter(
            ready=True,
            checked_at__gte=timezone.now() - timedelta(seconds=30),
            fingerprint=configured_fingerprint(),
        ).exists()
    )


def available():
    return (
        worker_available()
        and not QueueLock.objects.filter(id=1, pause_token__isnull=False).exists()
    )


def lock_queue():
    # All callers hold transaction.atomic; the row also serializes quota checks.
    QueueLock.objects.get_or_create(id=1)
    return QueueLock.objects.select_for_update().get(id=1)


@transaction.atomic
def pause_admission(token):
    token = uuid.UUID(str(token))
    control = lock_queue()
    if control.pause_token not in (None, token):
        raise AdmissionError("Another deployment owns the admission pause.", 409)
    if control.pause_token is None:
        control.pause_token = token
        control.paused_at = timezone.now()
        control.save(update_fields=["pause_token", "paused_at"])


@transaction.atomic
def resume_admission(token):
    token = uuid.UUID(str(token))
    control = lock_queue()
    if control.pause_token is None:
        return
    if control.pause_token != token:
        raise AdmissionError("Only the owning deployment can resume admission.", 409)
    if not worker_available():
        raise AdmissionError("A fresh matching worker must be ready before admission resumes.")
    if (
        Submission.objects.filter(status__in=["queued", "running"])
        .exclude(evaluation_fingerprint=configured_fingerprint())
        .exists()
    ):
        raise AdmissionError("Pending work from another evaluation policy must finish first.")
    control.pause_token = None
    control.paused_at = None
    control.save(update_fields=["pause_token", "paused_at"])


@transaction.atomic
def admission_state():
    control = QueueLock.objects.select_for_update().filter(id=1).first()
    fingerprint = configured_fingerprint()
    pending = Submission.objects.filter(status__in=["queued", "running"])
    return {
        "admission": "paused" if control and control.pause_token else "open",
        "token": str(control.pause_token) if control and control.pause_token else None,
        "paused_at": control.paused_at.isoformat() if control and control.paused_at else None,
        "queued": pending.filter(status="queued").count(),
        "running": pending.filter(status="running").count(),
        "incompatible_pending": pending.exclude(evaluation_fingerprint=fingerprint).count(),
        "worker_available": worker_available(),
        "fingerprint": fingerprint,
    }


def expire_jobs():
    now = timezone.now()
    expired = Submission.objects.filter(status=Submission.Status.RUNNING, lease_until__lt=now)
    expired.update(
        status="finished",
        verdict="internal_error",
        finished_at=now,
        result={"label": LABELS["internal_error"]},
    )
    Submission.objects.filter(status="queued", created_at__lt=now - timedelta(minutes=10)).update(
        status="finished",
        verdict="internal_error",
        finished_at=now,
        result={"label": "等待评测超时，请稍后重新提交。"},
    )


@transaction.atomic
def enqueue(user, version, source, mode):
    lock_queue()
    if not available():
        raise AdmissionError("评测暂不可用。你仍可阅读题目和保存草稿。")
    if not Problem.objects.filter(id=version.problem_id, current=version, published=True).exists():
        raise AdmissionError("题目已更新或下线，请刷新后重新提交。", 409)
    expire_jobs()
    active = Submission.objects.filter(status__in=["queued", "running"])
    if active.filter(user=user).count() >= settings.JUDGE_MAX_PENDING:
        raise AdmissionError("你已有待处理任务，请等待结果后继续。", 429)
    if active.count() >= settings.JUDGE_MAX_QUEUE:
        raise AdmissionError("评测队列已满，请稍后重试。", 429)
    return Submission.objects.create(
        user=user,
        version=version,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        evaluation_fingerprint=configured_fingerprint(),
        mode=mode,
    )


@transaction.atomic
def claim(fingerprint=None):
    expire_jobs()
    # PostgreSQL supports SKIP LOCKED; SQLite IMMEDIATE serializes this transaction.
    from django.db import connection

    query = Submission.objects.filter(
        status="queued", evaluation_fingerprint=fingerprint or configured_fingerprint()
    ).order_by("created_at")
    query = (
        query.select_for_update(skip_locked=True)
        if connection.features.has_select_for_update_skip_locked
        else query
    )
    job = query.first()
    if job is None:
        return None
    job.status = "running"
    job.started_at = timezone.now()
    job.lease_until = timezone.now() + timedelta(seconds=settings.JUDGE_LEASE_SECONDS)
    job.lease_token = uuid.uuid4()
    job.save(update_fields=["status", "started_at", "lease_until", "lease_token"])
    return job


def renew(job):
    return Submission.objects.filter(
        id=job.id, status="running", lease_token=job.lease_token
    ).update(lease_until=timezone.now() + timedelta(seconds=settings.JUDGE_LEASE_SECONDS))


def finish(job, result, environment):
    return Submission.objects.filter(
        id=job.id, status="running", lease_token=job.lease_token
    ).update(
        status="finished",
        verdict=result["verdict"],
        result=result,
        environment=environment,
        finished_at=timezone.now(),
        lease_until=None,
    )
