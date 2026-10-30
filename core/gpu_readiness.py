"""Complete supervised startup before publishing readiness; no web registration."""

import json
import math
import re
import time
import uuid

from core import gpu_resources
from core.gpu_control import bounded_database
from core.gpu_startup import recover_controller, startup_identity
from core.models import GPUDevice
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_isolation import validate_structure
from judge.gpu_kernel_checks import CHECKS as KERNEL_CHECKS
from judge.gpu_kernel_checks import check_request, validate_check_report
from judge.gpu_lifecycle import CHECKS as CONTROLLER_CHECKS
from judge.gpu_lifecycle import FAULT_SECONDS
from judge.gpu_monitor import DeviceMonitor, identify
from judge.gpu_receipt import (
    CHECK_SECONDS,
    CHECKS,
    SUITE_SECONDS,
    ReceiptStore,
    binding,
    check_receipt,
    require,
    validate_receipt,
)
from judge.gpu_service import OwnedJobService


def _identity(guard, worker_id, adapter):
    if (
        not GPUDevice.objects.select_for_update()
        .filter(
            pk=guard.device_uuid,
            owner_token=guard.owner_token,
            worker_id=worker_id,
            fingerprint=adapter.fingerprint,
        )
        .first()
    ):
        raise gpu_resources.DeviceOwnershipError("GPU readiness database identity changed")


def _stage_report(name, report, adapter):
    require(type(report) is dict, "GPU startup stage did not return a report")
    if name == "initial-recovery":
        stopped = report.get("stopped")
        require(
            report.get("recovered") is True
            and report.get("ready") is False
            and report.get("environment") == adapter.environment
            and type(stopped) is list
            and len(stopped) == (5 if report.get("previous_job") else 2)
            and all(
                type(row) is dict
                and row.get("stopped") is True
                and row.get("cgroup_empty") is True
                and type(row.get("unit")) is str
                for row in stopped
            ),
            "Initial GPU recovery did not verify all owned services",
        )
        require(
            len({row["unit"] for row in stopped}) == len(stopped),
            "Initial GPU recovery duplicated a service",
        )
    elif name == "resources":
        validate_structure(report, adapter.target)
    else:
        require(
            type(report.get("challenge")) is str
            and re.fullmatch(r"[a-f0-9]{32}", report["challenge"]),
            "GPU startup stage is missing its challenge",
        )
        category, case = name.split("/", 1)
        if category == "kernel":
            validate_check_report(report, check_request(case, report["challenge"]))
        else:
            require(
                category == "controller"
                and case in CONTROLLER_CHECKS
                and set(report)
                == {"name", "challenge", "disconnect_seconds", "independent_stop", "recovered"}
                and report["name"] == case
                and report["recovered"] is True
                and report["independent_stop"] is (case != "transport-loss")
                and type(report["disconnect_seconds"]) in {int, float}
                and math.isfinite(report["disconnect_seconds"])
                and 0 < report["disconnect_seconds"] < FAULT_SECONDS,
                "Incomplete controller fault or recovery report",
            )


def deactivate_controller(guard, *, receipt_directory):
    """Revoke this owner's readiness; retain any task until verified recovery."""
    failures = []
    try:
        with ReceiptStore(receipt_directory, guard) as store:
            if store.read().get("owner_token") == str(guard.owner_token):
                store.invalidate()
    except FileNotFoundError:
        pass
    except Exception as error:
        failures.append(error)
    try:
        with bounded_database():
            gpu_resources.quarantine(guard)
    except Exception as error:
        failures.append(error)
    if failures:
        from judge.sandbox import SandboxUnavailable

        raise SandboxUnavailable("GPU readiness could not be fully revoked") from failures[0]


def _fail_closed(error, guard, directory):
    # Keep the original failure, and never change a replacement owner's state.
    try:
        deactivate_controller(guard, receipt_directory=directory)
    except Exception as cleanup_error:
        error.add_note("GPU readiness revocation failed: " + type(cleanup_error).__name__)


def initialize_controller(
    guard, worker_id, adapter, *, receipt_directory, deadline=None, on_progress=None, on_tick=None
):
    """Every invocation performs all real checks; never resume from a file/report.

    Unit tests may replace runtime methods but do not produce operational GPU
    evidence. Every worker start uses this entry; web admission remains separate.
    """
    if deadline is None:
        deadline = time.monotonic() + SUITE_SECONDS
    completed, evidence = [], []

    def record(name, report):
        require(name == CHECKS[len(completed)], "GPU startup checks ran out of order")
        _stage_report(name, report, adapter)
        evidence.append({"name": name, "report": report})
        completed.append(name)
        if on_progress is not None:
            on_progress(name)

    try:
        binding(guard, worker_id, adapter)
        if (
            type(deadline) not in {int, float}
            or not math.isfinite(deadline)
            or not 0 < deadline - time.monotonic() <= SUITE_SECONDS
        ):
            raise ValueError("GPU startup requires a bounded complete-suite deadline")
        recovery = recover_controller(
            guard,
            worker_id,
            adapter,
            deadline=min(deadline, time.monotonic() + CHECK_SECONDS),
            on_tick=on_tick,
        )
        require(
            recovery["recovered"] is True
            and recovery["ready"] is False
            and recovery["environment"] == adapter.environment,
            "Initial GPU recovery did not verify the configured environment",
        )
        job = uuid.UUID(recovery["previous_job"]) if recovery["previous_job"] else None
        reservation = (
            uuid.UUID(recovery["previous_reservation"])
            if recovery["previous_reservation"]
            else None
        )

        def tick():
            guard.assert_held()
            if on_tick is not None:
                on_tick()
            require(time.monotonic() < deadline, "Complete GPU startup deadline expired")
            with bounded_database():
                gpu_resources.verify_recovery_owner(
                    guard, job_id=job, reservation_token=reservation
                )
                _identity(guard, worker_id, adapter)
                if on_tick is not None:
                    on_tick()
            require(time.monotonic() < deadline, "Complete GPU startup deadline expired")

        tick()
        with ReceiptStore(receipt_directory, guard, create=True) as store:
            store.invalidate()
            record("initial-recovery", recovery)
            startup_job, startup_reservation = startup_identity(guard.device_uuid)

            def run(name, method, *args):
                tick()
                service = OwnedJobService(
                    guard, startup_job, startup_reservation, purpose="maintenance"
                )
                try:
                    report, environment = method(
                        service,
                        *args,
                        deadline=min(deadline, time.monotonic() + CHECK_SECONDS),
                        on_tick=tick,
                    )
                finally:
                    service.stop()
                tick()
                require(environment == adapter.environment, "GPU startup check changed environment")
                record(name, report)

            run("resources", adapter.check_resources)
            for name in KERNEL_CHECKS:
                run("kernel/" + name, adapter.check_kernel, name)
            for name in CONTROLLER_CHECKS:
                run("controller/" + name, adapter.check_controller, name)
            tick()
            require(tuple(completed) == CHECKS, "GPU startup did not complete its full suite")
            value = {
                **binding(guard, worker_id, adapter),
                "created_wall": time.time(),
                "created_monotonic": time.monotonic(),
                "evidence_digest": canonical_digest(evidence),
            }
            validate_receipt(value, guard, worker_id, adapter)
            store._write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
            validate_receipt(store.read(), guard, worker_id, adapter)
            # Database fencing is repeated after file publication; an old slot
            # cannot be replaced between the checks and the ready transition.
            with bounded_database():
                gpu_resources.verify_recovery_owner(
                    guard, job_id=job, reservation_token=reservation
                )
                _identity(guard, worker_id, adapter)
                if on_tick is not None:
                    on_tick()
                require(time.monotonic() < deadline, "Complete GPU startup deadline expired")
                gpu_resources.confirm_recovered(guard, job_id=job, reservation_token=reservation)
        return value
    except BaseException as error:
        _fail_closed(error, guard, receipt_directory)
        raise


def _idle(guard, worker_id, adapter, receipt_directory, *, claim, on_check):
    try:
        check_receipt(receipt_directory, guard, worker_id, adapter)
        require(identify(guard.device_uuid) == adapter.target, "GPU identity changed after startup")
        DeviceMonitor(adapter.target).observe()
        with bounded_database():
            _identity(guard, worker_id, adapter)
            check_receipt(receipt_directory, guard, worker_id, adapter)
            if on_check is not None:
                on_check()
            healthy = gpu_resources.heartbeat(guard)
            result = gpu_resources.claim(guard) if claim else healthy
            if on_check is not None:
                on_check()
            return result
    except BaseException as error:
        _fail_closed(error, guard, receipt_directory)
        raise


def heartbeat_ready(guard, worker_id, adapter, *, receipt_directory):
    """An expired/missing/currently mismatched receipt cannot refresh readiness."""
    return _idle(guard, worker_id, adapter, receipt_directory, claim=False, on_check=None)


def claim_ready(guard, worker_id, adapter, *, receipt_directory, on_check=None):
    """Heartbeat and claim under the same checked device identity row lock."""
    return _idle(guard, worker_id, adapter, receipt_directory, claim=True, on_check=on_check)


def recover_cancelled(guard, worker_id, adapter, job, *, receipt_directory, on_tick):
    """The original execution service has stopped; now recover the exact slot."""
    report = recover_controller(
        guard, worker_id, adapter, on_tick=on_tick, expected_slot=(job.id, job.lease_token)
    )
    _stage_report("initial-recovery", report, adapter)
    require(
        (report["previous_job"], report["previous_reservation"])
        == (str(job.id), str(job.lease_token)),
        "GPU reservation changed before cancellation recovery",
    )
    with bounded_database():
        _identity(guard, worker_id, adapter)
        gpu_resources.verify_recovery_owner(guard, job_id=job.id, reservation_token=job.lease_token)
        on_tick()
        check_receipt(receipt_directory, guard, worker_id, adapter)
        gpu_resources.confirm_recovered(guard, job_id=job.id, reservation_token=job.lease_token)
