"""Disposable candidate submissions through the ordinary GPU queue, never host execution."""

import hashlib
import re
import time
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from core.content import digest
from core.gpu_admission import valid_environment
from core.gpu_resources import FRESH_SECONDS
from core.management.commands.deployment_check import candidate_ready, paused_gate, smoke_problem
from core.management.commands.judge_admission import database_action
from core.models import GPUDevice, Problem, ProblemVersion, QueueLock, Submission
from core.queue import (
    AdmissionError,
    configured_fingerprint,
    enqueue,
    pause_admission,
    resume_admission,
)
from core.resources import GPU_BUDGET_POLICY, GPU_SLOT_SECONDS
from core.result_evidence import InvalidResult, verified_evidence
from judge.container_environment import BACKEND
from judge.gpu_policy import execution_digest

PROTOCOL = "gpu-deployment-check-v1"
WAIT_SECONDS = 2 * GPU_SLOT_SECONDS["submit"] + 120
CORRECT = (
    'extern "C" __global__ void solve(const float* a, const float* b, float* out, int n) {\n'
    "    int i = blockIdx.x * blockDim.x + threadIdx.x;\n"
    "    if (i < n) out[i] = a[i] + b[i];\n"
    "}\n"
)
WRONG = CORRECT.replace("a[i] + b[i]", "a[i] - b[i]")
SOURCES = (CORRECT, WRONG)
VERDICTS = ("accepted", "wrong_answer")


def identifier(token, worker, fingerprint):
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"{PROTOCOL}:{token}:{worker}:{fingerprint}")
    return "gpu-deploy-check-" + value.hex


def problem_data(name):
    value = smoke_problem(name)
    value.pop("function")
    value.update(
        title="GPU deployment verification",
        summary="Disposable real CUDA queue verification.",
        statement="Add the two input vectors element by element.",
        constraints="Fixed float32 vectors with 3 or 257 entries.",
        language="cuda",
        evaluator="cuda-kernel-v1",
        operation="vector-add-f32-v1",
        entrypoint="solve",
        starter=CORRECT,
        reference=CORRECT,
        hints=["Add corresponding entries with a bounds check."],
        comparison={"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        tests=[
            {
                "name": "public",
                "public": True,
                "inputs": {"a": [1, -2, 3], "b": [4, 5, 6]},
                "expected": [5, 3, 9],
            },
            {
                "name": "protected-tail",
                "public": False,
                "inputs": {"a": list(range(-128, 129)), "b": [2] * 257},
                "expected": list(range(-126, 131)),
            },
        ],
    )
    return value


def unrelated_relations(value, allowed):
    """Reject any non-fixture rows that deletion would cascade or invalidate."""
    for field in value._meta.get_fields(include_hidden=True):
        if (
            field.auto_created
            and not field.concrete
            and field.is_relation
            and field.related_model not in allowed
            and field.related_model._default_manager.filter(**{field.field.name: value.pk}).exists()
        ):
            return True
    return False


class CandidateProbe:
    def __init__(self, *, token, worker, cpu_worker, device, owner, fingerprint, started):
        try:
            self.token, self.owner = uuid.UUID(str(token)), uuid.UUID(str(owner))
            self.started = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if (
                not timezone.is_aware(self.started)
                or self.started > timezone.now()
                or any(
                    type(name) is not str
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name)
                    for name in (worker, cpu_worker)
                )
                or type(device) is not str
                or not device.startswith("GPU-")
                or str(uuid.UUID(device[4:])) != device[4:]
                or type(fingerprint) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CommandError(
                "GPU candidate check requires exact startup and device identities."
            ) from None
        if (
            not settings.JUDGE_ENABLED
            or settings.JUDGE_BACKEND != "wasi"
            or tuple(settings.JUDGE_GPU_DEVICES) != (device,)
        ):
            raise CommandError(
                "GPU candidate check requires the enabled, single configured device."
            )
        self.worker, self.cpu_worker, self.device = worker, cpu_worker, device
        self.fingerprint = fingerprint
        self.name = identifier(self.token, worker, fingerprint)
        self.data = problem_data(self.name)
        self.policy = execution_digest()

    def gate(self):
        control = paused_gate(self.token)
        # Briefly exclude registration/heartbeat writes while examining all slots.
        # No device or queue lock is held across waiting for actual execution.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("LOCK TABLE core_gpudevice IN SHARE ROW EXCLUSIVE MODE")
        devices = list(GPUDevice.objects.select_for_update().order_by("pk")[:513])
        device = next((row for row in devices if row.pk == self.device), None)
        if (
            len(devices) > 512
            or device is None
            or device.worker_id != self.worker
            or device.owner_token != self.owner
            or device.fingerprint != self.fingerprint
            or type(device.environment) is not dict
            or device.environment.get("backend") != BACKEND
            or not valid_environment(device, policy=self.policy)
        ):
            raise CommandError("Original GPU candidate ownership or environment changed.")
        return control, devices, device

    def ready(self, device):
        now = timezone.now()
        return (
            device.state == GPUDevice.State.READY
            and device.job_id is None
            and device.reservation_token is None
            and device.checked_at is not None
            and self.started <= device.checked_at <= now
            and now - device.checked_at < timedelta(seconds=FRESH_SECONDS)
        )

    def recognize(self):
        actor = get_user_model().objects.select_for_update().filter(username=self.name).first()
        problem = Problem.objects.select_for_update().filter(pk=self.name).first()
        versions = list(ProblemVersion.objects.select_for_update().filter(problem_id=self.name))
        if actor is None and problem is None and not versions:
            return None
        if (
            actor is None
            or actor.has_usable_password()
            or not actor.is_active
            or actor.is_staff
            or actor.is_superuser
            or actor.last_login is not None
            or actor.email
            or actor.first_name
            or actor.last_name
            or actor.groups.exists()
            or actor.user_permissions.exists()
            or unrelated_relations(actor, {Submission})
            or problem is None
            or not problem.published
            or unrelated_relations(problem, {ProblemVersion})
            or len(versions) != 1
            or versions[0].revision != 1
            or versions[0].data != self.data
            or versions[0].digest != digest(self.data)
            or problem.current_id != versions[0].pk
            or unrelated_relations(versions[0], {Problem, Submission})
            or Problem.objects.filter(current=versions[0]).exclude(pk=self.name).exists()
        ):
            raise CommandError("GPU candidate fixture has unknown ownership or related data.")
        version = versions[0]
        jobs = list(Submission.objects.select_for_update().filter(version=version))
        if (
            len(jobs) != 2
            or actor.submission_set.count() != 2
            or {job.source for job in jobs} != set(SOURCES)
            or any(
                job.user_id != actor.pk
                or job.mode != "submit"
                or job.source_digest != hashlib.sha256(job.source.encode()).hexdigest()
                or job.evaluation_fingerprint != self.fingerprint
                or job.budget_policy != GPU_BUDGET_POLICY
                or job.budget_units != GPU_SLOT_SECONDS["submit"]
                or unrelated_relations(job, set())
                for job in jobs
            )
        ):
            raise CommandError("GPU candidate submissions are not exclusively owned fixtures.")
        by_source = {job.source: job for job in jobs}
        return actor, problem, version, [by_source[source] for source in SOURCES]

    def prepare(self):
        control, devices, device = self.gate()
        owned = self.recognize()
        pending = Submission.objects.filter(status__in=["queued", "running"])
        if owned is not None:
            if pending.exclude(pk__in=[job.pk for job in owned[3]]).exists():
                raise CommandError("Unrelated work must drain before GPU candidate verification.")
            return owned[3]
        if (
            pending.exists()
            or any(row.job_id is not None for row in devices)
            or not self.ready(device)
        ):
            raise CommandError(
                "GPU candidate preparation requires fresh readiness and complete drain."
            )
        candidate_ready(self.cpu_worker, configured_fingerprint())
        actor = get_user_model().objects.create_user(username=self.name, password=None)
        problem = Problem.objects.create(pk=self.name)
        version = ProblemVersion.objects.create(
            problem=problem, revision=1, data=self.data, digest=digest(self.data)
        )
        problem.current = version
        problem.save(update_fields=["current"])
        resume_admission(self.token)
        jobs = [enqueue(actor, version, source, "submit") for source in SOURCES]
        pause_admission(self.token)
        QueueLock.objects.filter(pk=1).update(paused_at=control.paused_at)
        return jobs

    def finish(self, *, cleanup=False):
        _, devices, device = self.gate()
        owned = self.recognize()
        if owned is None and not cleanup:
            raise CommandError("Original GPU candidate submissions disappeared.")
        if (
            Submission.objects.filter(status__in=["queued", "running"]).exists()
            or (owned is not None and any(job.status != "finished" for job in owned[3]))
            or any(row.job_id is not None for row in devices)
            or not self.ready(device)
        ):
            if cleanup:
                raise CommandError(
                    "GPU candidate cleanup requires terminal jobs and physical recovery."
                )
            return None
        if not cleanup:
            self.remaining(owned[3])
            candidate_ready(self.cpu_worker, configured_fingerprint())
            for job, expected in zip(owned[3], VERDICTS, strict=True):
                try:
                    metrics = verified_evidence(job)
                except InvalidResult:
                    raise CommandError(
                        "GPU candidate formal evidence is invalid; fixtures retained."
                    ) from None
                if (
                    metrics["verdict"] != expected
                    or metrics["total"] != 2
                    or metrics["passed"] != (2 if expected == "accepted" else 0)
                    or job.environment
                    != device.environment | {"language": "cuda", "evaluator": "cuda-kernel-v1"}
                    or not self.started
                    <= job.created_at
                    <= job.started_at
                    <= job.finished_at
                    <= timezone.now()
                ):
                    raise CommandError(
                        "GPU candidate result or startup identity differs; fixtures retained."
                    )
        if cleanup and owned is not None:
            actor, problem, version, _ = owned
            actor.delete()
            problem.current = None
            problem.save(update_fields=["current"])
            version.delete()
            problem.delete()
        return {
            "protocol": PROTOCOL,
            "action": "cleanup" if cleanup else "check",
            "passed": not cleanup,
            "token": str(self.token),
            "worker_id": self.worker,
            "cpu_worker_id": self.cpu_worker,
            "device": self.device,
            "owner_token": str(self.owner),
            "fingerprint": self.fingerprint,
            "started_at": self.started.isoformat(),
            "fixture": self.name,
            "verdicts": [] if cleanup else list(VERDICTS),
            "admission": "paused",
            "cleanup_passed": cleanup,
        }

    def remaining(self, jobs):
        age = (timezone.now() - min(job.created_at for job in jobs)).total_seconds()
        if age < 0:
            raise CommandError("GPU candidate creation time is in the future.")
        if age >= WAIT_SECONDS:
            raise CommandError(
                "GPU candidate observation timed out; original submissions retained."
            )
        return WAIT_SECONDS - age

    def run(self, *, cleanup=False, timeout=WAIT_SECONDS, database_timeout=5):
        if not 0 <= timeout <= WAIT_SECONDS or not 0 < database_timeout <= 60:
            raise CommandError("Invalid GPU candidate observation or database timeout.")
        try:
            if cleanup:
                return database_action(lambda: self.finish(cleanup=True), database_timeout)
            jobs = database_action(self.prepare, database_timeout)
            deadline = time.monotonic() + min(timeout, self.remaining(jobs))
            while True:
                report = database_action(self.finish, database_timeout)
                if report is not None:
                    return report
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError(
                        "GPU candidate observation timed out; original submissions retained."
                    )
                time.sleep(min(0.2, remaining))
        except AdmissionError as error:
            raise CommandError(str(error)) from error
