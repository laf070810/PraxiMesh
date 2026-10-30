"""Conservative sandbox-call reservations, never measured CPU or a billing ledger."""

from datetime import UTC

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import Submission

BUDGET_POLICY = "cpu-invocations-v1"
GPU_BUDGET_POLICY = "gpu-slot-seconds-v1"
GPU_EVALUATOR = "cuda-kernel-v1"
GPU_SLOT_SECONDS = {"run": 90, "submit": 180}
# Each invocation has at most the existing 15-second wall allowance. stdio has
# separate compile and execution calls; content cannot override these costs.
CALLS_PER_CASE = {
    "python-function-v1": 1,
    "python-object-api-v1": 1,
    "javascript-function-v1": 1,
    "python-stdio-v1": 2,
    "python-ml-linear-v1": 1,
    "python-weighted-fit-v1": 1,
    "python-agent-tools-v1": 6,
}


def reservation_units(version, mode):
    evaluator = version.data.get("evaluator")
    cases = version.data.get("tests")
    if evaluator == GPU_EVALUATOR:
        if (
            mode not in GPU_SLOT_SECONDS
            or not isinstance(cases, list)
            or not 2 <= len(cases) <= 8
            or any(type(case) is not dict or type(case.get("public")) is not bool for case in cases)
            or not any(mode == "submit" or case["public"] for case in cases)
        ):
            raise ValueError("Invalid GPU reservation contract.")
        return GPU_SLOT_SECONDS[mode]
    if (
        evaluator not in CALLS_PER_CASE
        or mode not in {"run", "submit"}
        or not isinstance(cases, list)
    ):
        raise ValueError("Unsupported CPU reservation contract.")
    if (
        not 1
        <= len(cases)
        <= (
            8
            if evaluator
            in {"python-ml-linear-v1", "python-weighted-fit-v1", "python-agent-tools-v1"}
            else 20
        )
    ):
        raise ValueError("Invalid CPU reservation case count.")
    if any(type(case) is not dict or type(case.get("public")) is not bool for case in cases):
        raise ValueError("Invalid CPU reservation visibility.")
    count = sum(mode == "submit" or case["public"] for case in cases)
    if not count:
        raise ValueError("A CPU reservation needs at least one selected case.")
    return count * CALLS_PER_CASE[evaluator]


def utc_day(now=None):
    return (now or timezone.now()).astimezone(UTC).date()


def usage(user, day=None, *, policy=BUDGET_POLICY):
    day = day or utc_day()
    if policy not in {BUDGET_POLICY, GPU_BUDGET_POLICY}:
        raise ValueError("Unsupported resource budget policy.")
    limit = (
        settings.JUDGE_DAILY_GPU_SECONDS
        if policy == GPU_BUDGET_POLICY
        else settings.JUDGE_DAILY_CPU_UNITS
    )
    totals = Submission.objects.filter(user=user, budget_day=day, budget_policy=policy).aggregate(
        reserved=Sum("budget_units", filter=Q(budget_state="reserved")),
        consumed=Sum("budget_units", filter=Q(budget_state="consumed")),
    )
    reserved, consumed = totals["reserved"] or 0, totals["consumed"] or 0
    return {
        "policy": policy,
        "day": day.isoformat(),
        "limit": limit,
        "reserved": reserved,
        "consumed": consumed,
        "remaining": max(0, limit - reserved - consumed),
    }


def reservation_data(job):
    return {
        "policy": job.budget_policy,
        "day": job.budget_day.isoformat() if job.budget_day else None,
        "units": job.budget_units,
        "state": job.budget_state,
        "execution_started_at": job.execution_started_at.isoformat()
        if job.execution_started_at
        else None,
    }
