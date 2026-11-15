"""Bounded worker-side GPU lease callbacks, still outside submission admission.

Physical termination, device idleness and recovery are separate requirements.
Only a trusted isolation controller calls the after_recovery methods after all
three have actually succeeded. Database tests do not provide that evidence.
"""

import time
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone

from core import gpu_resources
from core.resources import GPU_BUDGET_POLICY, reservation_units
from judge.gpu_device_lock import DeviceLock
from judge.gpu_service import RESERVE_SECONDS
from judge.gpu_session import GPUExecutionError
from judge.sandbox import SandboxUnavailable

LOCK_MILLISECONDS = 250
STATEMENT_MILLISECONDS = 1000


class GPUJobLost(SandboxUnavailable):
    pass


@contextmanager
def bounded_database():
    """Bound server-side work/lock waits without changing other connections.

    This is not a network deadline. Owned task services enforce their own hard
    runtime limit even if a database socket or this Python process stops responding.
    """
    if connection.in_atomic_block:
        raise ValueError("GPU control requires its own short transaction")
    if connection.vendor == "postgresql":
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, true)", [f"{LOCK_MILLISECONDS}ms"]
                )
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{STATEMENT_MILLISECONDS}ms"],
                )
            yield
    elif connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            previous = int(cursor.fetchone()[0])
            cursor.execute(f"PRAGMA busy_timeout={LOCK_MILLISECONDS}")
        try:
            with transaction.atomic():
                yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"PRAGMA busy_timeout={previous}")
    else:
        raise SandboxUnavailable("Unsupported GPU control database")


class GPUJobControl:
    def __init__(self, guard, job, *, on_check=None):
        if type(guard) is not DeviceLock:
            raise ValueError("A held device lock is required")
        guard.assert_held()
        if (
            job.status != "running"
            or job.started_at is None
            or job.lease_token is None
            or job.budget_policy != GPU_BUDGET_POLICY
            or job.budget_units != reservation_units(job.version, job.mode)
        ):
            raise ValueError("A claimed GPU reservation is required")
        elapsed = max(0, (timezone.now() - job.started_at).total_seconds())
        self.deadline = time.monotonic() + job.budget_units - elapsed
        self.guard, self.job = guard, job
        self.on_check = on_check
        self._check()

    def _check(self, *, cleanup=False):
        self.guard.assert_held()
        deadline = self.deadline if cleanup else self.deadline - RESERVE_SECONDS
        if time.monotonic() >= deadline:
            raise GPUExecutionError("time_limit")

    def _call(self, function, *, cleanup=False):
        self._check(cleanup=cleanup)
        with bounded_database():
            self._check(cleanup=cleanup)
            if self.on_check is not None:
                self.on_check()
            result = function()
            # Roll back a renewal if a lock wait crossed the task's slot budget.
            self._check(cleanup=cleanup)
            if self.on_check is not None:
                self.on_check()
        return result

    def poll(self):
        if not self._call(lambda: gpu_resources.renew(self.guard, self.job)):
            raise GPUJobLost("GPU task was cancelled, deleted or lost its lease")

    def begin_execution(self):
        if not self._call(lambda: gpu_resources.begin_execution(self.guard, self.job)):
            raise GPUJobLost("GPU task cannot begin execution with an expired lease")

    def quarantine(self):
        # Terminate the owned service first; this write may itself fail or wait.
        self.guard.assert_held()
        with bounded_database():
            if not gpu_resources.quarantine(self.guard, self.job):
                raise GPUJobLost("GPU reservation has changed")

    def finish_after_recovery(self, result, environment):
        """Persist a result and free this slot only AFTER real physical recovery."""

        def finish():
            changed = gpu_resources.finish(self.guard, self.job, result, environment)
            gpu_resources.confirm_recovered(
                self.guard, job_id=self.job.id, reservation_token=self.job.lease_token
            )
            return changed

        return self._call(finish, cleanup=True)

    def release_after_recovery(self):
        """Recover a cancelled/deleted job; never write an accepted result."""
        self.guard.assert_held()
        with bounded_database():
            if not gpu_resources.quarantine(self.guard, self.job):
                raise GPUJobLost("GPU reservation has changed")
            gpu_resources.confirm_recovered(
                self.guard, job_id=self.job.id, reservation_token=self.job.lease_token
            )
