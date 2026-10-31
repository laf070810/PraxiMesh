"""Recover an owned GPU pool before readiness; no receipt or ready transition here."""

import math
import os
import time

from core import gpu_resources
from core.gpu_container_recovery import BoundContainerRecovery, validate_journal
from core.gpu_control import bounded_database
from core.gpu_identity import startup_identity
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from judge.gpu_namespace import JobWorkspace
from judge.gpu_recovery import recover_workspace
from judge.gpu_service import OwnedJobService
from judge.sandbox import SandboxUnavailable

STARTUP_SECONDS = 180
MAINTENANCE_SECONDS = 90


def clear_workspace(directory, service, *, deadline, on_tick):
    """Caller has stopped every recognized service before deleting any directory."""
    name = service.unit.removeprefix("praximesh-gpu-").removesuffix(".service")
    if not os.path.lexists(directory / name):
        return False
    with JobWorkspace(directory, service, create=False) as workspace:
        on_tick()
        try:
            workspace.remove_after_stop()
        except PermissionError:
            recover_workspace(workspace, deadline=deadline, on_tick=on_tick)
    return True


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
    """Adopt, stop old owned work and run fixed maintenance, retaining quarantine.

    The caller holds the device lock throughout later readiness checks as well.
    Successful return is physical recovery only, never admission authorization.
    """
    if type(guard) is not DeviceLock or type(adapter) is not GPUExecutionAdapter:
        raise ValueError("A held device lock and supervised adapter are required")
    guard.assert_held()
    if container_journal is not None:
        validate_journal(container_journal, guard)
    if adapter.target["uuid"] != guard.device_uuid:
        raise ValueError("Startup recovery must use the locked device")
    if deadline is None:
        deadline = time.monotonic() + STARTUP_SECONDS
    if (
        type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or not 0 < deadline - time.monotonic() <= STARTUP_SECONDS
    ):
        raise ValueError("Startup recovery requires a finite bounded deadline")
    try:
        with bounded_database():
            device = gpu_resources.adopt(guard, worker_id, adapter.fingerprint)
            if (
                expected_slot is not None
                and (device.job_id, device.reservation_token) != expected_slot
            ):
                raise gpu_resources.DeviceOwnershipError("GPU reservation changed before recovery")
            gpu_resources.quarantine(guard)
            job_id, reservation_token = device.job_id, device.reservation_token
    except BaseException:
        # The journal independently identifies an owned original invocation.
        # Stop it on DB failure, retaining the record and every DB reservation.
        if container_journal is not None:
            container_journal.stop()
        raise

    def tick():
        guard.assert_held()
        if on_tick is not None:
            on_tick()
        if time.monotonic() >= deadline:
            raise SandboxUnavailable("GPU startup recovery deadline expired")
        with bounded_database():
            gpu_resources.verify_recovery_owner(
                guard, job_id=job_id, reservation_token=reservation_token
            )
        if time.monotonic() >= deadline:
            raise SandboxUnavailable("GPU startup recovery deadline expired")

    startup_job, startup_reservation = startup_identity(guard.device_uuid)
    identities = [(startup_job, startup_reservation, ("maintenance", "recovery"))]
    if job_id is not None:
        identities.insert(0, (job_id, reservation_token, ("task", "maintenance", "recovery")))
    services = [
        OwnedJobService(guard, job, reservation, purpose=purpose)
        for job, reservation, purposes in identities
        for purpose in purposes
    ]
    record = {
        "previous_job": str(job_id) if job_id else None,
        "previous_reservation": str(reservation_token) if reservation_token else None,
        "stopped": [],
        "removed": [],
    }
    # Database failure must not keep the old workload alive. Once the exact old
    # reservation is known, stop its services before doing any further DB work.
    failures = []
    for service in services:
        try:
            record["stopped"].append(service.stop())
        except Exception as error:
            failures.append(error)
    container_recovery = None
    if container_journal is not None:
        try:
            container_recovery = BoundContainerRecovery(container_journal)
            container_recovery.stop()
        except Exception as error:
            failures.append(error)
    if failures:
        raise SandboxUnavailable("An owned startup service could not be stopped") from failures[0]
    tick()
    if container_recovery is not None:
        record["container_recovery"] = container_recovery.finish(
            job_id=job_id,
            reservation_token=reservation_token,
            on_tick=tick,
        )
    # Remove old filesystem helpers first, so a mapped-store recovery can create
    # a fresh helper with that reservation's deterministic identity.
    for service in sorted(services, key=lambda item: item.purpose != "recovery"):
        if clear_workspace(adapter.directory, service, deadline=deadline, on_tick=tick):
            record["removed"].append(service.unit)
    tick()
    # A new invocation is intentional only after every old service stopped and
    # its private workspace was removed. Do not reuse a prior InvocationID fence.
    maintenance = OwnedJobService(guard, startup_job, startup_reservation, purpose="maintenance")
    try:
        environment = adapter.recover(
            maintenance,
            deadline=min(deadline, time.monotonic() + MAINTENANCE_SECONDS),
            on_tick=tick,
        )
    finally:
        maintenance.stop()
    tick()
    record.update(
        maintenance_unit=maintenance.unit, environment=environment, recovered=True, ready=False
    )
    return record
