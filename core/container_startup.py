"""Recover the original container reservation before complete startup publication."""

import math
import time

from core import gpu_resources
from core.gpu_container_recovery import BoundContainerRecovery, validate_journal
from core.gpu_control import bounded_database
from core.gpu_evidence import environment_profile
from core.gpu_identity import startup_identity
from core.models import GPUDevice
from core.resources import GPU_EVALUATOR
from judge.container_adapter import ContainerGPUAdapter
from judge.container_environment import BACKEND
from judge.container_monitor import ContainerDeviceMonitor
from judge.container_readiness import SCOPE, task_identity
from judge.controller_faults import require
from judge.gpu_device_lock import DeviceLock


def compatible_previous(device, adapter, record):
    """A container cannot clean a pending host service or an unknown old backend."""
    if device is None:
        return True
    try:
        environment_profile(
            {**device.environment, "language": "cuda", "evaluator": GPU_EVALUATOR},
            device.fingerprint,
        )
    except (ValueError, TypeError, KeyError):
        if device.environment != {}:
            return False
    else:
        if device.environment["backend"] == BACKEND:
            return True
        return (
            device.state == GPUDevice.State.READY
            and device.job_id is None
            and device.reservation_token is None
        )
    if device.fingerprint == adapter.fingerprint:
        return True
    # An interrupted unpublished container start still has its frozen v3
    # device mapping and exact task slot, even across source/image upgrades.
    if record is not None and record["version"] == 3:
        expected = (
            (device.job_id, device.reservation_token)
            if device.job_id is not None
            else startup_identity(adapter.target["uuid"])
        )
        return (record["job"], record["reservation"]) == tuple(map(str, expected))
    return False


def recover_controller(
    guard,
    worker_id,
    adapter,
    *,
    deadline=None,
    on_tick=None,
    expected_slot=None,
    container_journal=None,
):
    if (
        type(guard) is not DeviceLock
        or type(adapter) is not ContainerGPUAdapter
        or adapter.control.guard is not guard
    ):
        raise ValueError("Container startup requires the original held lock and adapter")
    guard.assert_held()
    journal = adapter.control.journal
    if container_journal is not None and container_journal is not journal:
        raise ValueError("Container startup cannot replace its original journal")
    validate_journal(journal, guard)
    if deadline is None:
        deadline = time.monotonic() + 180
    if (
        type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or not 0 < deadline - time.monotonic() <= 180
    ):
        raise ValueError("Container startup requires a finite bounded deadline")
    require(
        adapter.target["uuid"] == guard.device_uuid, "Container startup changed its locked device"
    )
    old = BoundContainerRecovery(journal)
    try:
        with bounded_database():
            previous = GPUDevice.objects.select_for_update().filter(pk=guard.device_uuid).first()
            compatible = compatible_previous(previous, adapter, old.record)
            require(
                compatible,
                "Recover the previous host or unknown backend before container startup",
            )
            device = gpu_resources.adopt(guard, worker_id, adapter.fingerprint)
            if (
                expected_slot is not None
                and (device.job_id, device.reservation_token) != expected_slot
            ):
                raise gpu_resources.DeviceOwnershipError("GPU reservation changed before recovery")
            gpu_resources.quarantine(guard)
            job, reservation = device.job_id, device.reservation_token
    except BaseException:
        old.stop()
        raise
    old.stop()

    def tick():
        adapter.control.check()
        if on_tick is not None:
            on_tick()
        require(time.monotonic() < deadline, "Container startup recovery deadline expired")
        with bounded_database():
            gpu_resources.verify_recovery_owner(guard, job_id=job, reservation_token=reservation)
        require(time.monotonic() < deadline, "Container startup recovery deadline expired")

    tick()
    cleanup = old.finish(job_id=job, reservation_token=reservation, on_tick=tick)
    adapter.check()
    ContainerDeviceMonitor(adapter.control).observe()
    startup_job, startup_reservation = startup_identity(guard.device_uuid)
    task = adapter.task(guard, startup_job, startup_reservation, purpose="maintenance")
    try:
        environment = adapter.recover(
            task, deadline=min(deadline, time.monotonic() + 90), on_tick=tick
        )
    finally:
        if task.cleanup is None:
            task.recover()
    tick()
    adapter.check()
    ContainerDeviceMonitor(adapter.control).observe()
    return {
        "scope": SCOPE,
        "previous_job": str(job) if job else None,
        "previous_reservation": str(reservation) if reservation else None,
        "container_recovery": cleanup,
        "maintenance": task_identity(task),
        "environment": environment,
        "recovered": True,
        "ready": False,
    }
