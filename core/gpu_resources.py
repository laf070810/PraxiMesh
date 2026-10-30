"""GPU ownership and task leases; release only after controller-verified recovery.

These primitives do not open submission admission or perform hardware probes.
The trusted GPU worker must hold the shared host lock and perform real recovery
before calling confirm_recovered. A stale DB lease never makes a device free.
"""

import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from core.models import GPUDevice, Submission
from core.resources import GPU_BUDGET_POLICY, GPU_EVALUATOR, reservation_units
from judge.gpu_device_lock import DeviceLock
from judge.results import LABELS

FRESH_SECONDS = 30


class DeviceOwnershipError(RuntimeError):
    pass


def _guard(guard):
    if type(guard) is not DeviceLock:
        raise DeviceOwnershipError("A real GPU host ownership lock is required")
    guard.assert_held()


def _device(guard):
    _guard(guard)
    device = GPUDevice.objects.select_for_update().filter(pk=guard.device_uuid).first()
    _guard(guard)
    if device is None or device.owner_token != guard.owner_token:
        raise DeviceOwnershipError("GPU controller ownership changed")
    return device


@transaction.atomic
def adopt(guard, worker_id, fingerprint):
    """A new host-lock owner starts quarantined and retains any old reservation."""
    _guard(guard)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", worker_id) or not re.fullmatch(
        r"[a-f0-9]{64}", fingerprint
    ):
        raise ValueError("Invalid GPU worker identity")
    device = GPUDevice.objects.select_for_update().filter(pk=guard.device_uuid).first()
    guard.assert_held()
    if device is None:
        return GPUDevice.objects.create(
            id=guard.device_uuid,
            owner_token=guard.owner_token,
            worker_id=worker_id,
            fingerprint=fingerprint,
        )
    if device.owner_token == guard.owner_token:
        if device.worker_id != worker_id or device.fingerprint != fingerprint:
            raise DeviceOwnershipError("A live controller cannot change its execution identity")
        return device
    device.owner_token = guard.owner_token
    device.worker_id = worker_id
    device.fingerprint = fingerprint
    device.environment = {}
    device.state = GPUDevice.State.QUARANTINED
    device.checked_at = None
    device.save(
        update_fields=[
            "owner_token",
            "worker_id",
            "fingerprint",
            "environment",
            "state",
            "checked_at",
        ]
    )
    return device


@transaction.atomic
def confirm_recovered(guard, *, job_id, reservation_token):
    """Record a completed physical recovery, fencing the exact observed slot.

    Only the trusted controller calls this, after terminating its owned workload,
    checking device idleness and passing a fresh correctness/recovery probe.
    """
    device = _device(guard)
    if (device.job_id, device.reservation_token) != (job_id, reservation_token):
        raise DeviceOwnershipError("GPU reservation changed during recovery")
    if device.state != GPUDevice.State.QUARANTINED:
        raise DeviceOwnershipError("GPU must be quarantined before recovery completes")
    if device.job_id is not None:
        current = Submission.objects.select_for_update().filter(pk=device.job_id).first()
        if current is not None:
            if (
                current.lease_token != device.reservation_token
                or current.budget_policy != GPU_BUDGET_POLICY
            ):
                raise DeviceOwnershipError("Task identity no longer matches the recovering GPU")
            if current.status != Submission.Status.FINISHED:
                current.status = Submission.Status.FINISHED
                current.verdict = "internal_error"
                current.result = {"label": LABELS["internal_error"]}
                current.finished_at = timezone.now()
                current.lease_until = None
                if current.budget_state == "reserved":
                    current.budget_state = "released"
                current.save(
                    update_fields=[
                        "status",
                        "verdict",
                        "result",
                        "finished_at",
                        "lease_until",
                        "budget_state",
                    ]
                )
    device.state = GPUDevice.State.READY
    device.checked_at = timezone.now()
    device.job_id = device.reservation_token = None
    device.save(update_fields=["state", "checked_at", "job_id", "reservation_token"])


@transaction.atomic
def heartbeat(guard):
    device = _device(guard)
    if device.state != GPUDevice.State.READY:
        return False
    device.checked_at = timezone.now()
    device.save(update_fields=["checked_at"])
    return True


def available(device_uuid, fingerprint):
    now = timezone.now()
    device = GPUDevice.objects.filter(
        pk=device_uuid,
        fingerprint=fingerprint,
        state__in=[GPUDevice.State.READY, GPUDevice.State.RESERVED],
        checked_at__gte=now - timedelta(seconds=FRESH_SECONDS),
    ).first()
    if device is None:
        return False
    return (
        device.state == GPUDevice.State.READY
        or Submission.objects.filter(
            id=device.job_id,
            lease_token=device.reservation_token,
            lease_until__gt=now,
            status=Submission.Status.RUNNING,
            evaluation_fingerprint=fingerprint,
            budget_policy=GPU_BUDGET_POLICY,
        ).exists()
    )


@transaction.atomic
def claim(guard):
    device = _device(guard)
    now = timezone.now()
    if (
        device.state != GPUDevice.State.READY
        or device.checked_at is None
        or device.checked_at < now - timedelta(seconds=FRESH_SECONDS)
    ):
        return None
    query = Submission.objects.filter(
        status=Submission.Status.QUEUED,
        created_at__gte=now - timedelta(minutes=10),
        evaluation_fingerprint=device.fingerprint,
        budget_policy=GPU_BUDGET_POLICY,
        budget_state="reserved",
        version__data__evaluator=GPU_EVALUATOR,
        version__data__language="cuda",
    ).order_by("created_at")
    if connection.features.has_select_for_update_skip_locked:
        query = query.select_for_update(of=("self",), skip_locked=True)
    job = query.first()
    if job is None:
        return None
    if job.budget_units != reservation_units(job.version, job.mode):
        raise ValueError("GPU task reservation does not match its fixed execution budget")
    job.status = Submission.Status.RUNNING
    job.started_at = now
    job.lease_until = now + timedelta(seconds=settings.JUDGE_LEASE_SECONDS)
    job.lease_token = uuid.uuid4()
    job.save(update_fields=["status", "started_at", "lease_until", "lease_token"])
    device.state = GPUDevice.State.RESERVED
    device.job_id = job.id
    device.reservation_token = job.lease_token
    device.checked_at = now
    device.save(update_fields=["state", "job_id", "reservation_token", "checked_at"])
    return job


def _current(guard, job):
    device = _device(guard)
    if (
        device.state != GPUDevice.State.RESERVED
        or device.job_id != job.id
        or device.reservation_token != job.lease_token
        or device.fingerprint != job.evaluation_fingerprint
    ):
        return device, None
    current = (
        Submission.objects.select_for_update()
        .filter(
            id=job.id,
            status=Submission.Status.RUNNING,
            lease_token=job.lease_token,
            lease_until__gt=timezone.now(),
            evaluation_fingerprint=device.fingerprint,
            budget_policy=GPU_BUDGET_POLICY,
            budget_state__in=["reserved", "consumed"],
        )
        .first()
    )
    # The SQL predicate uses a time captured before waiting for the task row.
    # A task can cross its deadline while another transaction owns that row.
    _guard(guard)
    if current is not None and current.lease_until <= timezone.now():
        current = None
    if current is None:
        device.state = GPUDevice.State.QUARANTINED
        device.checked_at = None
        device.save(update_fields=["state", "checked_at"])
    return device, current


def _renew(device, current, *, execution):
    now = timezone.now()
    fields = ["lease_until"]
    if execution and current.budget_state == "reserved":
        current.budget_state = "consumed"
        current.execution_started_at = now
        fields += ["budget_state", "execution_started_at"]
    current.lease_until = now + timedelta(seconds=settings.JUDGE_LEASE_SECONDS)
    current.save(update_fields=fields)
    device.checked_at = now
    device.save(update_fields=["checked_at"])
    return True


@transaction.atomic
def begin_execution(guard, job):
    device, current = _current(guard, job)
    return _renew(device, current, execution=True) if current is not None else False


@transaction.atomic
def renew(guard, job):
    device, current = _current(guard, job)
    return _renew(device, current, execution=False) if current is not None else False


@transaction.atomic
def finish(guard, job, result, environment):
    from core.queue import _finish_current

    device, current = _current(guard, job)
    if current is None:
        return 0
    changed = _finish_current(job, current, result, environment)
    device.state = GPUDevice.State.QUARANTINED
    device.checked_at = None
    device.save(update_fields=["state", "checked_at"])
    return changed


@transaction.atomic
def quarantine(guard, job=None):
    device = _device(guard)
    if job is not None and (device.job_id, device.reservation_token) != (job.id, job.lease_token):
        return False
    device.state = GPUDevice.State.QUARANTINED
    device.checked_at = None
    device.save(update_fields=["state", "checked_at"])
    return True


@transaction.atomic
def verify_recovery_owner(guard, *, job_id, reservation_token):
    """Fence startup work without announcing health or changing the old slot."""
    device = _device(guard)
    if (
        device.state != GPUDevice.State.QUARANTINED
        or device.checked_at is not None
        or (device.job_id, device.reservation_token) != (job_id, reservation_token)
    ):
        raise DeviceOwnershipError("GPU ownership or reservation changed during startup recovery")
