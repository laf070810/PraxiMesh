import hashlib
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext

from core.models import GPUDevice, Problem, QueueLock, Submission, WorkerHealth
from core.resources import (
    BUDGET_POLICY,
    GPU_BUDGET_POLICY,
    GPU_EVALUATOR,
    reservation_units,
    usage,
    utc_day,
)
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


def supports(version):
    from judge.evaluator import EVALUATORS

    evaluator = version.data.get("evaluator")
    if evaluator == GPU_EVALUATOR:
        from core.gpu_admission import configured

        return version.data.get("language") == "cuda" and configured()
    return evaluator in EVALUATORS and (
        settings.JUDGE_BACKEND == "wasi"
        or (settings.JUDGE_BACKEND == "gvisor" and evaluator == "python-function-v1")
    )


def available(version=None):
    if version is not None and version.data.get("evaluator") == GPU_EVALUATOR:
        return supports(version) and gpu_available()
    return (
        (version is None or supports(version))
        and worker_available()
        and not QueueLock.objects.filter(id=1, pause_token__isnull=False).exists()
    )


def gpu_available():
    from core.gpu_admission import candidates

    return (
        bool(candidates())
        and not QueueLock.objects.filter(id=1, pause_token__isnull=False).exists()
    )


def _compatible_pending(pending, devices):
    cpu = (
        Q(evaluation_fingerprint=configured_fingerprint())
        & ~Q(budget_policy=GPU_BUDGET_POLICY)
        & ~Q(version__data__evaluator=GPU_EVALUATOR)
    )
    gpu = Q(
        evaluation_fingerprint__in=[device.fingerprint for device in devices],
        budget_policy=GPU_BUDGET_POLICY,
        version__data__evaluator=GPU_EVALUATOR,
        version__data__language="cuda",
    )
    return pending.filter(cpu | gpu)


def _gpu_reservations(devices):
    # All devices, including ones removed from configuration. A deleted account
    # or finished task cannot erase the need for physical recovery.
    reservations = GPUDevice.objects.filter(job_id__isnull=False)
    healthy = [device.pk for device in devices if device.state == GPUDevice.State.RESERVED]
    return reservations.count(), reservations.exclude(pk__in=healthy).count()


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
    from core.gpu_admission import candidates, configured

    devices = candidates()
    if configured() and not devices:
        raise AdmissionError("A fresh matching GPU worker must be ready before admission resumes.")
    pending = Submission.objects.filter(status__in=["queued", "running"])
    if pending.exclude(pk__in=_compatible_pending(pending, devices).values("pk")).exists():
        raise AdmissionError("Pending work from another evaluation policy must finish first.")
    if _gpu_reservations(devices)[1]:
        raise AdmissionError(
            "Unrecovered GPU reservations must be resolved before admission resumes."
        )
    control.pause_token = None
    control.paused_at = None
    control.save(update_fields=["pause_token", "paused_at"])


@transaction.atomic
def admission_state():
    from core.gpu_admission import candidates, configured

    control = QueueLock.objects.select_for_update().filter(id=1).first()
    fingerprint = configured_fingerprint()
    pending = Submission.objects.filter(status__in=["queued", "running"])
    devices = candidates()
    reserved, incompatible_reservations = _gpu_reservations(devices)
    return {
        "admission": "paused" if control and control.pause_token else "open",
        "token": str(control.pause_token) if control and control.pause_token else None,
        "paused_at": control.paused_at.isoformat() if control and control.paused_at else None,
        "queued": pending.filter(status="queued").count(),
        "running": pending.filter(status="running").count(),
        "incompatible_pending": pending.exclude(
            pk__in=_compatible_pending(pending, devices).values("pk")
        ).count(),
        "worker_available": worker_available(),
        "fingerprint": fingerprint,
        "gpu_configured": configured(),
        "gpu_worker_available": bool(devices),
        "gpu_reserved": reserved,
        "incompatible_gpu_reservations": incompatible_reservations,
    }


@transaction.atomic
def expire_jobs():
    now = timezone.now()
    expired = Submission.objects.filter(status=Submission.Status.RUNNING, lease_until__lt=now)
    _expire(expired, now, LABELS["internal_error"])
    _expire(
        Submission.objects.filter(status="queued", created_at__lt=now - timedelta(minutes=10)),
        now,
        "等待评测超时，请稍后重新提交。",
    )


def _expire(query, now, label):
    # Each SQL transition rechecks status/lease and reservation state after row
    # locking, so a concurrent first call can never be refunded as unstarted.
    query.filter(budget_state="reserved").update(
        status="finished",
        verdict="internal_error",
        finished_at=now,
        result={"label": label},
        budget_state="released",
        lease_until=None,
    )
    query.exclude(budget_state="reserved").update(
        status="finished",
        verdict="internal_error",
        finished_at=now,
        result={"label": label},
        lease_until=None,
    )


@transaction.atomic
def enqueue(user, version, source, mode):
    lock_queue()
    gpu = version.data.get("evaluator") == GPU_EVALUATOR
    target = None
    if gpu:
        from core.gpu_admission import candidates, select_target

        if (
            supports(version)
            and not QueueLock.objects.filter(id=1, pause_token__isnull=False).exists()
        ):
            target = select_target(candidates())
    if (gpu and target is None) or (not gpu and not available(version)):
        raise AdmissionError(gettext("评测暂不可用。你仍可阅读题目和保存草稿。"))
    if not Problem.objects.filter(id=version.problem_id, current=version, published=True).exists():
        raise AdmissionError(gettext("题目已更新或下线，请刷新后重新提交。"), 409)
    expire_jobs()
    active = Submission.objects.filter(status__in=["queued", "running"])
    if active.filter(user=user).count() >= settings.JUDGE_MAX_PENDING:
        raise AdmissionError(gettext("你已有待处理任务，请等待结果后继续。"), 429)
    if active.count() >= settings.JUDGE_MAX_QUEUE:
        raise AdmissionError(gettext("评测队列已满，请稍后重试。"), 429)
    day = utc_day()
    try:
        units = reservation_units(version, mode)
    except ValueError:
        raise AdmissionError(gettext("该题的资源预约策略无效，暂不能评测。")) from None
    policy = GPU_BUDGET_POLICY if gpu else BUDGET_POLICY
    if units > usage(user, day, policy=policy)["remaining"]:
        message = (
            gettext("今日 GPU 整卡时段预约额度不足，请在下个 UTC 日重试。")
            if gpu
            else gettext("今日 CPU 练习预约额度不足，请在下个 UTC 日重试。")
        )
        raise AdmissionError(message, 429)
    return Submission.objects.create(
        user=user,
        version=version,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        evaluation_fingerprint=target.fingerprint if gpu else configured_fingerprint(),
        mode=mode,
        budget_policy=policy,
        budget_day=day,
        budget_units=units,
        budget_state="reserved",
    )


@transaction.atomic
def claim(fingerprint=None):
    expire_jobs()
    # PostgreSQL supports SKIP LOCKED; SQLite IMMEDIATE serializes this transaction.
    from django.db import connection

    query = (
        Submission.objects.filter(
            status="queued", evaluation_fingerprint=fingerprint or configured_fingerprint()
        )
        .exclude(budget_policy=GPU_BUDGET_POLICY)
        .exclude(version__data__evaluator=GPU_EVALUATOR)
        .order_by("created_at")
    )
    query = (
        query.select_for_update(of=("self",), skip_locked=True)
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


def _current_cpu(job):
    # Only lock the task: published versions remain read-only to workers.
    current = (
        Submission.objects.select_for_update(of=("self",))
        .filter(
            id=job.id, status="running", lease_token=job.lease_token, lease_until__gt=timezone.now()
        )
        .exclude(budget_policy=GPU_BUDGET_POLICY)
        .exclude(version__data__evaluator=GPU_EVALUATOR)
        .first()
    )
    # Waiting for this row must never extend an expired execution lease.
    if current is None or current.lease_until <= timezone.now():
        return None
    return current


@transaction.atomic
def renew(job):
    current = _current_cpu(job)
    if current is None:
        return 0
    current.lease_until = timezone.now() + timedelta(seconds=settings.JUDGE_LEASE_SECONDS)
    current.save(update_fields=["lease_until"])
    return 1


@transaction.atomic
def begin_execution(job):
    current = _current_cpu(job)
    if current is None or current.budget_state == "released":
        return False
    now = timezone.now()
    fields = ["lease_until"]
    if current.budget_state == "reserved":
        current.budget_state = "consumed"
        current.execution_started_at = now
        fields.extend(["budget_state", "execution_started_at"])
    # Grandfathered work predating this migration is never retrospectively charged.
    current.lease_until = now + timedelta(seconds=settings.JUDGE_LEASE_SECONDS)
    current.save(update_fields=fields)
    return True


@transaction.atomic
def finish(job, result, environment):
    current = _current_cpu(job)
    if current is None:
        return 0
    return _finish_current(job, current, result, environment)


def _finish_current(job, current, result, environment):
    # Both CPU and GPU paths hold the current task row lock. GPU callers also
    # hold their device row and matching host ownership/reservation fence.
    updates = {}
    if current.budget_state == "reserved":
        if result["verdict"] != "internal_error":
            raise ValueError("A non-platform result requires a recorded execution start.")
        updates["budget_state"] = "released"
    environment = {
        **environment,
        "language": job.version.data["language"],
        "evaluator": job.version.data["evaluator"],
    }
    now = timezone.now()
    return Submission.objects.filter(
        id=job.id, status="running", lease_token=job.lease_token, lease_until__gt=now
    ).update(
        **updates,
        status="finished",
        verdict=result["verdict"],
        result=result,
        environment=environment,
        finished_at=now,
        lease_until=None,
    )
