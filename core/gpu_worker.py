"""GPU worker lifecycle. Runtime doubles are confined to tests, never this entry."""

import logging
import signal
import time
from contextlib import contextmanager

from core.gpu_control import GPUJobLost
from core.gpu_execution import execute_claimed
from core.gpu_readiness import (
    _identity,
    claim_ready,
    deactivate_controller,
    initialize_controller,
    recover_cancelled,
)
from judge.gpu_receipt import GPUReceiptExpired, check_receipt

logger = logging.getLogger(__name__)


class WorkerStopping(Exception):
    pass


class StopFlag:
    """Main-thread signal flag, deliberately without any synchronization lock."""

    def __init__(self):
        self.requested = False

    def is_set(self):
        return self.requested

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True


@contextmanager
def stop_signals(stop):
    """Main thread only. Handlers perform no DB, filesystem or service work."""
    if type(stop) is not StopFlag:
        raise ValueError("Signal handlers require a lock-free StopFlag")
    previous = {}

    def request_stop(_signum, _frame):
        stop.requested = True

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_worker(guard, worker_id, adapter, *, receipt_directory, stop, once=False):
    """Caller retains the host lock until this function's shutdown completes."""

    def stopping():
        if stop.is_set():
            raise WorkerStopping

    def current():
        stopping()
        return check_receipt(receipt_directory, guard, worker_id, adapter, minimum_remaining=0)

    def executing():
        # Called INSIDE GPUJobControl's bounded transaction. Lock identity before
        # renewing, beginning execution or settling; recheck before commit too.
        current()
        _identity(guard, worker_id, adapter)

    def initialize():
        initialize_controller(
            guard,
            worker_id,
            adapter,
            receipt_directory=receipt_directory,
            on_tick=stopping,
            on_progress=lambda stage: logger.info("gpu_startup_passed stage=%s", stage),
        )
        logger.info("gpu_worker_ready")

    failure = None
    try:
        initialize()
        while True:
            stopping()
            try:
                job = claim_ready(
                    guard,
                    worker_id,
                    adapter,
                    receipt_directory=receipt_directory,
                    on_check=stopping,
                )
            except GPUReceiptExpired:
                logger.info("gpu_startup_refresh reason=receipt_expiry")
                initialize()
                continue
            if job is not None:
                try:
                    changed = execute_claimed(guard, job, adapter, on_check=executing)
                except GPUJobLost:
                    logger.info("gpu_result_discarded job=%s reason=lease_lost", job.id)
                    try:
                        recover_cancelled(
                            guard,
                            worker_id,
                            adapter,
                            job,
                            receipt_directory=receipt_directory,
                            on_tick=current,
                        )
                    except GPUReceiptExpired:
                        initialize()
                else:
                    logger.info("gpu_task_settled job=%s recorded=%s", job.id, bool(changed))
            if once:
                return
            # The lock-free flag is checked at most every 100 ms while idle.
            stop.wait(1)
    except WorkerStopping:
        logger.info("gpu_worker_stopping")
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            deactivate_controller(guard, receipt_directory=receipt_directory)
        except Exception as error:
            if failure is None:
                raise
            failure.add_note("GPU worker shutdown failed: " + type(error).__name__)
