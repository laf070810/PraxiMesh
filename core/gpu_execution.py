"""Connect claimed GPU tasks to supervised execution, keeping admission closed."""

from core.gpu_control import GPUJobControl
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable


def execute_claimed(guard, job, adapter):
    """Settle only after the adapter completes physical cleanup and recovery.

    Caller retains the host lock and performs startup recovery/readiness before
    claiming work. There is deliberately no management command or receipt bypass.
    """
    if type(adapter) is not GPUExecutionAdapter:
        raise ValueError("The supervised GPU execution adapter is required")
    control = GPUJobControl(guard, job)
    service = OwnedJobService(guard, job.id, job.lease_token)
    try:
        if job.evaluation_fingerprint != adapter.fingerprint:
            raise SandboxUnavailable("GPU task execution fingerprint changed")
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
        control.quarantine()
        raise
