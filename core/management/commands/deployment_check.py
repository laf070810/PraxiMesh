"""Run isolated, disposable real submissions while public traffic is in maintenance."""

import copy
import hashlib
import json
import re
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.content import digest, load_package
from core.management.commands.judge_admission import database_action
from core.models import Problem, ProblemVersion, QueueLock, Submission, WorkerHealth
from core.queue import (
    AdmissionError,
    configured_fingerprint,
    enqueue,
    pause_admission,
    resume_admission,
)
from judge.sandbox import policy_digest
from judge.wasi_policy import POLICY_VERSION, WALL_SECONDS

CORRECT = "def solve(value):\n    return value + 1\n"
WRONG = "def solve(value):\n    return value\n"


def candidate_ready(worker_id, fingerprint):
    now = timezone.now()
    if not WorkerHealth.objects.filter(
        id=worker_id,
        ready=True,
        checked_at__gte=now - timedelta(seconds=30),
        checked_at__lte=now + timedelta(seconds=5),
        fingerprint=fingerprint,
    ).exists():
        raise CommandError("The named candidate worker is not freshly ready for this policy.")


def paused_gate(token):
    control = QueueLock.objects.select_for_update().filter(pk=1).first()
    if control is None or control.pause_token != token:
        raise CommandError("Deployment check requires the matching persistent admission pause.")
    return control


def smoke_problem(identifier):
    # Content is parsed as data. Neither this command nor content validation
    # imports evaluator paths or executes reference/user source on the host.
    package = load_package(settings.CONTENT_ROOT / "starter.json")
    problem = copy.deepcopy(package["problems"][0])
    problem.update(
        id=identifier,
        revision=1,
        title="Deployment verification",
        summary="Disposable real evaluation check.",
        statement="Implement solve(value), returning value + 1.",
        constraints="value is an integer between -10 and 10.",
        outcomes=["Verify the isolated evaluation service."],
        concepts=["functions"],
        language="python",
        evaluator="python-function-v1",
        function="solve",
        starter="def solve(value):\n    pass\n",
        reference=CORRECT,
        hints=["Return the argument plus one."],
        tags=["deployment-check"],
        tests=[
            {"name": "public", "args": [0], "expected": 1, "public": True},
            {"name": "protected", "args": [-7], "expected": -6, "public": False},
        ],
        comparison={"kind": "exact", "absolute": 0, "relative": 0},
        changelog="Disposable deployment verification; removed after the check.",
    )
    return problem


def fixture_identifier(token, worker_id):
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"praximesh-deployment-check:{token}:{worker_id}")
    return "deploy-check-" + identity.hex


class Command(BaseCommand):
    help = "Verify the named WASI candidate using real disposable submissions under maintenance."

    def add_arguments(self, parser):
        parser.add_argument("--token", type=uuid.UUID, required=True)
        parser.add_argument("--worker-id", required=True)
        parser.add_argument("--timeout", type=float, default=60)
        parser.add_argument("--database-timeout", type=float, default=5)

    def handle(self, *args, **options):
        try:
            token = uuid.UUID(str(options["token"]))
            timeout = float(options["timeout"])
            database_timeout = float(options["database_timeout"])
        except (ValueError, TypeError, OverflowError) as error:
            raise CommandError("A valid UUID and finite timeouts are required.") from error
        if not 0 <= timeout <= 600 or not 0 < database_timeout <= 60:
            raise CommandError("--timeout must be 0..600 and --database-timeout must be >0..60.")
        worker_id = options["worker_id"]
        if not isinstance(worker_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", worker_id
        ):
            raise CommandError(
                "--worker-id must identify the candidate hostname (1..80 characters)."
            )
        if not settings.JUDGE_ENABLED or settings.JUDGE_BACKEND != "wasi":
            raise CommandError(
                "Deployment verification requires the explicitly enabled WASI backend."
            )
        if not re.fullmatch(r"[a-f0-9]{64}", settings.JUDGE_WASI_SHA256):
            raise CommandError("Deployment verification requires a pinned WASI manifest.")
        fingerprint, policy = configured_fingerprint(), policy_digest()
        identifier = fixture_identifier(token, worker_id)
        data = smoke_problem(identifier)
        owned = {}
        gate_owned = False

        def recognize_previous():
            nonlocal gate_owned
            paused_gate(token)
            gate_owned = True
            candidate_ready(worker_id, fingerprint)
            actor = get_user_model().objects.select_for_update().filter(username=identifier).first()
            problem = Problem.objects.select_for_update().filter(pk=identifier).first()
            versions = list(
                ProblemVersion.objects.select_for_update().filter(problem_id=identifier)
            )
            if actor is None and problem is None and not versions:
                return
            if (
                problem is None
                or len(versions) != 1
                or versions[0].revision != 1
                or versions[0].digest != digest(data)
                or versions[0].data != data
            ):
                raise CommandError(
                    "Existing deployment fixture does not match exact ownership data."
                )
            version = versions[0]
            if version.draft_set.exists():
                raise CommandError("Existing deployment fixture has unrelated private drafts.")
            jobs = list(Submission.objects.select_for_update().filter(version_id=version.pk))
            if actor is None:
                if problem.published or problem.current_id is not None or jobs:
                    raise CommandError(
                        "Existing revoked deployment fixture is not safely identifiable."
                    )
            else:
                if (
                    actor.has_usable_password()
                    or actor.is_staff
                    or actor.is_superuser
                    or not actor.is_active
                    or actor.last_login is not None
                    or actor.email
                    or actor.first_name
                    or actor.last_name
                    or actor.groups.exists()
                    or actor.user_permissions.exists()
                    or hasattr(actor, "accountprofile")
                    or actor.draft_set.exists()
                    or actor.enrollment_set.exists()
                    or not problem.published
                    or problem.current_id != version.pk
                    or len(jobs) != 2
                    or actor.submission_set.count() != 2
                    or {job.source for job in jobs} != {CORRECT, WRONG}
                    or any(
                        job.user_id != actor.pk
                        or job.mode != "submit"
                        or job.evaluation_fingerprint != fingerprint
                        or job.source_digest != hashlib.sha256(job.source.encode()).hexdigest()
                        for job in jobs
                    )
                ):
                    raise CommandError(
                        "Existing deployment account or jobs are not exclusively owned fixtures."
                    )
            unrelated = Submission.objects.filter(status__in=["queued", "running"])
            if actor is not None:
                unrelated = unrelated.exclude(user_id=actor.pk)
            if unrelated.exists():
                raise CommandError(
                    "Deployment verification requires all unrelated work to drain first."
                )
            # Populate cleanup authority only after every ownership check passes.
            owned.update(problem=identifier, version=version.pk, ack_required=actor is None)
            if actor is not None:
                owned["user"] = actor.pk

        def prepare():
            nonlocal gate_owned
            control = paused_gate(token)
            gate_owned = True
            if Submission.objects.filter(status__in=["queued", "running"]).exists():
                raise CommandError("Deployment verification requires a completely drained queue.")
            candidate_ready(worker_id, fingerprint)
            actor = get_user_model().objects.create_user(username=identifier, password=None)
            owned["user"] = actor.pk
            problem = Problem.objects.create(pk=identifier)
            owned["problem"] = identifier
            version = ProblemVersion.objects.create(
                problem=problem, revision=1, digest=digest(data), data=data
            )
            owned["version"] = version.pk
            problem.current = version
            problem.save(update_fields=["current"])
            # The queue lock spans this whole outer transaction. Other callers
            # cannot observe an open gate; workers see both jobs after re-pause.
            resume_admission(token)
            jobs = [enqueue(actor, version, source, "submit") for source in (CORRECT, WRONG)]
            pause_admission(token)
            QueueLock.objects.filter(pk=1).update(paused_at=control.paused_at)
            owned["jobs"] = [job.pk for job in jobs]

        def outcomes():
            paused_gate(token)
            candidate_ready(worker_id, fingerprint)
            jobs = list(Submission.objects.filter(pk__in=owned["jobs"], user_id=owned["user"]))
            if len(jobs) != 2:
                raise CommandError("Deployment verification submissions disappeared.")
            if any(job.status != "finished" for job in jobs):
                return False
            by_id = {job.pk: job for job in jobs}
            for job_id, expected in zip(owned["jobs"], ("accepted", "wrong_answer"), strict=True):
                job = by_id[job_id]
                expected_passed = 2 if expected == "accepted" else 0
                if (
                    job.verdict != expected
                    or not isinstance(job.result, dict)
                    or not isinstance(job.environment, dict)
                    or job.result.get("verdict") != expected
                    or job.result.get("total") != 2
                    or job.result.get("passed") != expected_passed
                    or job.evaluation_fingerprint != fingerprint
                    or job.environment.get("policy") != POLICY_VERSION
                    or job.environment.get("policy_digest") != policy
                    or job.environment.get("bundle") != settings.JUDGE_WASI_SHA256
                    or job.environment.get("host") != worker_id
                    or job.started_at is None
                    or job.finished_at is None
                    or job.lease_token is None
                ):
                    raise CommandError(
                        "Real candidate results or execution identity did not match."
                    )
            return True

        def revoke():
            # Deleting only this unusable synthetic account revokes any remaining
            # leases. An in-flight worker discards the result on its next heartbeat
            # or conditional finish, as with ordinary account deletion.
            running = owned.get("ack_required", False)
            previous_heartbeat = None
            if "user" in owned:
                running = running or any(
                    job.status == "running"
                    for job in Submission.objects.select_for_update().filter(
                        user_id=owned["user"], user__username=identifier
                    )
                )
            if running:
                previous_heartbeat = (
                    WorkerHealth.objects.select_for_update()
                    .filter(pk=worker_id, fingerprint=fingerprint)
                    .values_list("checked_at", flat=True)
                    .first()
                )
                owned["ack_required"] = True
            if "user" in owned:
                get_user_model().objects.filter(pk=owned["user"], username=identifier).delete()
            if "problem" in owned:
                Problem.objects.filter(pk=identifier).update(current=None, published=False)
            return running, previous_heartbeat

        def cleanup_content():
            if "version" in owned:
                ProblemVersion.objects.filter(pk=owned["version"], problem_id=identifier).delete()
            if "problem" in owned:
                Problem.objects.filter(pk=identifier).delete()
            if (
                get_user_model().objects.filter(username=identifier).exists()
                or Problem.objects.filter(pk=identifier).exists()
                or ProblemVersion.objects.filter(problem_id=identifier).exists()
            ):
                raise CommandError("Deployment verification cleanup did not complete.")

        def cleanup():
            if not owned:
                return
            running, previous_heartbeat = database_action(revoke, database_timeout)
            if running:
                # A worker may have claimed a job without loading its version yet.
                # Its health row was locked through revocation. A later update
                # therefore commits after revocation: a case heartbeat implies
                # version loading completed; an idle/exit heartbeat means the old
                # evaluation stopped. No cross-process wall-clock race is needed.
                cleanup_deadline = time.monotonic() + WALL_SECONDS + 5
                while previous_heartbeat is None or not database_action(
                    lambda: WorkerHealth.objects.filter(
                        pk=worker_id,
                        checked_at__gt=previous_heartbeat,
                        fingerprint=fingerprint,
                    ).exists(),
                    database_timeout,
                ):
                    if time.monotonic() >= cleanup_deadline:
                        raise CommandError(
                            "Synthetic leases were revoked but worker cleanup was not acknowledged; "
                            f"withdrawn diagnostic content remains: {identifier}. Admission stays paused."
                        )
                    time.sleep(0.2)
                owned["ack_required"] = False
            database_action(cleanup_content, database_timeout)
            owned.clear()

        try:
            database_action(recognize_previous, database_timeout)
            cleanup()
            database_action(prepare, database_timeout)
            deadline = time.monotonic() + timeout
            while not database_action(outcomes, database_timeout):
                if time.monotonic() >= deadline:
                    raise CommandError(
                        "Real candidate evaluation timed out; admission remains paused."
                    )
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        except AdmissionError as error:
            raise CommandError(str(error)) from error
        finally:
            if gate_owned:
                # Try cleanup even if a lost DB connection prevents pause
                # reconciliation. Any failure prevents a successful JSON receipt.
                try:
                    try:
                        database_action(lambda: pause_admission(token), database_timeout)
                    except AdmissionError as error:
                        raise CommandError(
                            "Admission ownership changed during candidate verification; "
                            "the other owner's pause was preserved."
                        ) from error
                finally:
                    cleanup()
        self.stdout.write(
            json.dumps(
                {
                    "passed": True,
                    "worker_id": worker_id,
                    "fingerprint": fingerprint,
                    "verdicts": ["accepted", "wrong_answer"],
                    "admission": "paused",
                    "cleanup_passed": True,
                },
                sort_keys=True,
            )
        )
