"""Connect claimed GPU tasks to supervised execution and current worker checks."""

from core.gpu_control import GPUJobControl
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable


def execute_claimed(guard, job, adapter, *, on_check=None):
    """Settle only after the adapter completes physical cleanup and recovery.

    Caller retains the host lock and performs startup recovery/readiness before
    claiming work. The worker supplies its receipt/identity/stop check, which
    GPUJobControl invokes inside each state transition's own short transaction.
    """
    if type(adapter) is not GPUExecutionAdapter:
        raise ValueError("The supervised GPU execution adapter is required")
    control = GPUJobControl(guard, job, on_check=on_check)
    service = OwnedJobService(guard, job.id, job.lease_token)
    try:
        if job.evaluation_fingerprint != adapter.fingerprint:
            raise SandboxUnavailable("GPU task execution fingerprint changed")
        control.poll()
        result, environment = adapter.evaluate(
            service,
            deadline=control.deadline,
            problem=job.version.data,
            source=job.source,
            mode=job.mode,
            on_tick=control.poll,
            on_begin=control.begin_execution,
        )
        return control.finish_after_recovery(result, environment)
    except BaseException:
        # A stuck DB transaction must not keep the submitted task alive. Stop
        # the exact owned service first, including pre-handshake failures.
        service.stop()
        try:
            control.quarantine()
        except Exception as error:
            # A cleanup failure must not look like a recoverable learner cancel.
            raise SandboxUnavailable("GPU task quarantine failed after service stop") from error
        raise
