"""One local public operation through complete startup and original cleanup.

Internal trusted-controller component. It does not initialize another physical
lock, publish readiness, access an application DB, or provide a host execution path.
"""

import time
import uuid
from copy import deepcopy

from judge.container_adapter import ContainerGPUAdapter
from judge.container_contract import MAX_SECONDS
from judge.container_monitor import ContainerDeviceMonitor
from judge.container_practice_state import LocalPracticeState, require
from judge.container_readiness import SCOPE, task_identity
from judge.container_startup_suite import run_startup
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_receipt import CHECK_SECONDS, CHECKS, SUITE_SECONDS
from judge.gpu_task_protocol import public_task_request


def execute_public(adapter, state, problem, source, *, on_tick, on_progress=None):
    if type(adapter) is not ContainerGPUAdapter or type(state) is not LocalPracticeState:
        raise ValueError("The original local state and container adapter are required")
    request = public_task_request(deepcopy(problem), source)
    control, guard = adapter.control, state.guard
    require(
        control.guard is guard and control.journal is state.journal,
        "Local execution changed its original controller",
    )
    control.check()
    adapter.check()
    environment = deepcopy(adapter.environment)
    fingerprint = adapter.fingerprint
    operation = state.begin(environment)
    job, reservation = (uuid.UUID(operation[key]) for key in ("job", "reservation"))
    startup_deadline = time.monotonic() + SUITE_SECONDS
    overall_deadline = startup_deadline + MAX_SECONDS

    def tick(expected=operation):
        on_tick()
        state.assert_operation(expected)
        guard.assert_held()
        require(
            adapter.control is control
            and control.guard is guard
            and control.journal is state.journal,
            "Local execution ownership changed",
        )
        control.check()
        adapter.check()
        require(
            adapter.fingerprint == fingerprint
            and adapter.environment == environment
            and time.monotonic() < overall_deadline,
            "Local execution environment or deadline changed",
        )

    def invoke(method, *, purpose, deadline, **kwargs):
        tick()
        task = adapter.task(guard, job, reservation, purpose=purpose)
        try:
            value = method(task, deadline=deadline, on_tick=tick, **kwargs)
        finally:
            if task.cleanup is None:
                task.recover()
        tick()
        identity = task_identity(task)
        return value, identity

    tick()
    ContainerDeviceMonitor(control).observe()
    recovered, identity = invoke(
        adapter.recover,
        purpose="maintenance",
        deadline=min(startup_deadline, time.monotonic() + CHECK_SECONDS),
    )
    require(recovered == environment, "Local initial recovery changed environment")
    initial = {
        "scope": SCOPE,
        "previous_job": None,
        "previous_reservation": None,
        "container_recovery": {"had_record": False, "cleanup": None, "ready": False},
        "maintenance": identity,
        "environment": recovered,
        "recovered": True,
        "ready": False,
    }
    evidence = run_startup(
        adapter,
        initial,
        job,
        reservation,
        deadline=startup_deadline,
        on_tick=tick,
        on_progress=on_progress,
    )
    tick()
    require(tuple(row["name"] for row in evidence) == CHECKS, "Local startup did not complete")
    result, _identity = invoke(
        adapter.evaluate_public,
        purpose="task",
        deadline=min(overall_deadline, time.monotonic() + MAX_SECONDS),
        problem=request["problem"],
        source=request["source"],
        on_begin=tick,
    )
    require(
        result["formal_result"] is False and result["environment"] == environment,
        "Local public result changed its scope or environment",
    )
    ContainerDeviceMonitor(control).observe()
    tick()
    completed = state.finish(operation)
    tick(completed)
    return {
        **deepcopy(result),
        "ready": False,
        "startup_checks": len(CHECKS),
        "startup_evidence_digest": canonical_digest(evidence),
    }
