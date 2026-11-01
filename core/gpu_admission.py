"""Read-only GPU admission from full-startup identity and current device leases."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from core.gpu_evidence import ENVIRONMENT_FIELDS, environment_profile
from core.gpu_resources import FRESH_SECONDS
from core.models import GPUDevice, Submission
from core.resources import GPU_BUDGET_POLICY, GPU_EVALUATOR, reservation_units
from judge.container_environment import BACKEND as CONTAINER_BACKEND
from judge.container_environment import FIELDS as CONTAINER_FIELDS
from judge.gpu_policy import execution_digest


def configured():
    return bool(settings.JUDGE_GPU_DEVICES)


def valid_environment(device, *, policy):
    try:
        environment = device.environment
        if (
            type(environment) is not dict
            or set(environment)
            != (
                CONTAINER_FIELDS
                if environment.get("backend") == CONTAINER_BACKEND
                else ENVIRONMENT_FIELDS
            )
            or environment["execution_digest"] != policy
            or environment["target"]["uuid"] != device.pk
        ):
            return False
        environment_profile(
            {**environment, "language": "cuda", "evaluator": GPU_EVALUATOR}, device.fingerprint
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    return True


def candidates():
    """A snapshot, never a physical reservation or a replacement for worker checks.

    A worker can become unhealthy after this read. Enqueued work retains its
    exact fingerprint; only the separately fenced GPU worker can execute it.
    Web deliberately needs SELECT alone on the device table.
    """
    if not settings.JUDGE_ENABLED or not configured():
        return []
    now = timezone.now()
    rows = list(
        GPUDevice.objects.filter(
            pk__in=settings.JUDGE_GPU_DEVICES,
            state__in=[GPUDevice.State.READY, GPUDevice.State.RESERVED],
            checked_at__gte=now - timedelta(seconds=FRESH_SECONDS),
            checked_at__lte=now,
        ).order_by("pk")
    )
    if not rows:
        return []
    try:
        policy = execution_digest()
    except OSError:
        # A CPU-only/incomplete package cannot attest this GPU execution policy.
        return []
    healthy = []
    for device in rows:
        if not valid_environment(device, policy=policy):
            continue
        if device.state == GPUDevice.State.READY:
            if device.job_id is None and device.reservation_token is None:
                healthy.append(device)
            continue
        job = (
            Submission.objects.filter(
                pk=device.job_id,
                lease_token=device.reservation_token,
                lease_until__gt=now,
                status=Submission.Status.RUNNING,
                evaluation_fingerprint=device.fingerprint,
                budget_policy=GPU_BUDGET_POLICY,
                budget_state__in=["reserved", "consumed"],
                version__data__evaluator=GPU_EVALUATOR,
                version__data__language="cuda",
            )
            .select_related("version")
            .defer("source")
            .first()
        )
        try:
            if job is not None and job.budget_units == reservation_units(job.version, job.mode):
                healthy.append(device)
        except (ValueError, TypeError):
            continue
    return healthy


def select_target(devices):
    if not devices:
        return None
    counts = {
        row["evaluation_fingerprint"]: row["count"]
        for row in Submission.objects.filter(
            status__in=["queued", "running"],
            evaluation_fingerprint__in=[device.fingerprint for device in devices],
        )
        .values("evaluation_fingerprint")
        .annotate(count=Count("pk"))
    }
    return min(devices, key=lambda device: (counts.get(device.fingerprint, 0), device.pk))
