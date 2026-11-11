"""Complete container startup evidence without database or readiness publication.

The caller owns initial recovery, durable task identities and its readiness gate.
Every call executes the full remaining suite; saved evidence cannot skip a stage.
"""

import math
import time
import uuid
from copy import deepcopy

from judge.container_adapter import ContainerGPUAdapter
from judge.container_readiness import SCOPE, stage_report, task_identity
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_kernel_checks import CHECKS as KERNEL_CHECKS
from judge.gpu_lifecycle import CHECKS as CONTROLLER_CHECKS
from judge.gpu_receipt import CHECK_SECONDS, CHECKS, SUITE_SECONDS, require


def run_startup(
    adapter,
    initial_recovery,
    job_id,
    reservation_token,
    *,
    deadline,
    on_tick,
    on_progress=None,
):
    if type(adapter) is not ContainerGPUAdapter:
        raise ValueError("The original container GPU adapter is required")
    now = time.monotonic()
    if (
        type(job_id) is not uuid.UUID
        or type(reservation_token) is not uuid.UUID
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or not now < deadline <= now + SUITE_SECONDS
    ):
        raise ValueError("Startup requires explicit maintenance identities and a bounded deadline")
    control = adapter.control
    guard, journal = control.guard, control.journal
    environment = deepcopy(adapter.environment)
    fingerprint = adapter.fingerprint
    evidence = []

    def tick():
        guard.assert_held()
        control.check()
        on_tick()
        require(time.monotonic() < deadline, "Complete container startup deadline expired")
        require(
            adapter.control is control
            and control.guard is guard
            and control.journal is journal
            and adapter.fingerprint == fingerprint
            and canonical_digest(adapter.environment) == fingerprint,
            "Container startup ownership or environment changed",
        )

    def record(name, report):
        tick()
        require(name == CHECKS[len(evidence)], "Container startup checks ran out of order")
        stage_report(name, report, adapter)
        evidence.append({"name": name, "report": deepcopy(report)})
        if on_progress is not None:
            on_progress(name)
        tick()

    def run(name, method, *args):
        tick()
        task = adapter.task(guard, job_id, reservation_token, purpose="maintenance")
        try:
            report, actual = method(
                task,
                *args,
                deadline=min(deadline, time.monotonic() + CHECK_SECONDS),
                on_tick=tick,
            )
        finally:
            if task.cleanup is None:
                task.recover()
        tick()
        require(actual == environment, "Container startup check changed environment")
        record(name, {"scope": SCOPE, "task": task_identity(task), "report": report})

    tick()
    adapter.check()
    require(journal.load() is None, "Recover the original task before complete startup")
    record("initial-recovery", initial_recovery)
    run("resources", adapter.check_resources)
    for name in KERNEL_CHECKS:
        run("kernel/" + name, adapter.check_kernel, name)
    for name in CONTROLLER_CHECKS:
        run("controller/" + name, adapter.check_controller, name)
    tick()
    adapter.check()
    require(journal.load() is None, "Container startup left an original task pending")
    require(tuple(row["name"] for row in evidence) == CHECKS, "Incomplete container startup suite")
    return evidence
