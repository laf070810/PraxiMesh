"""Bind durable container cleanup to a quarantined database slot, never readiness."""

import uuid

from core import gpu_resources
from core.gpu_control import bounded_database
from core.gpu_identity import startup_identity
from judge.container_journal import ContainerJournal
from judge.sandbox import SandboxUnavailable


def validate_journal(journal, guard):
    if type(journal) is not ContainerJournal or journal.guard is not guard:
        raise ValueError("Container recovery requires the same held device lock and journal")
    journal.check()


class BoundContainerRecovery:
    """Stop independently of database availability; archive only the fenced slot."""

    def __init__(self, journal):
        if type(journal) is not ContainerJournal:
            raise ValueError("An owned container journal is required")
        validate_journal(journal, journal.guard)
        self.journal = journal
        self.record = journal.load()
        self.stopped = False

    def unchanged(self):
        if self.journal.load() != self.record:
            raise SandboxUnavailable("Container recovery record changed during database fencing")

    def stop(self):
        self.unchanged()
        result = self.journal.stop()
        # stop() may fence an interrupted create by persisting its original ID.
        current = self.journal.load()
        if current != self.record:
            changed = {"container_id", "started_at", "phase"}
            if (
                self.record is None
                or current is None
                or self.record["container_id"] is not None
                or result is None
                or current["container_id"] != result["container"]
                or current["phase"] not in {"created", "running"}
                or {k: v for k, v in self.record.items() if k not in changed}
                != {k: v for k, v in current.items() if k not in changed}
            ):
                raise SandboxUnavailable("Container recovery record changed while stopping")
        self.record = current
        self.stopped = True
        return result

    def finish(self, *, job_id, reservation_token, on_tick=lambda: None):
        if not self.stopped:
            raise SandboxUnavailable("Stop the original container before database recovery")
        self.unchanged()
        if (job_id is None) != (reservation_token is None):
            raise gpu_resources.DeviceOwnershipError("Incomplete GPU recovery reservation")
        if self.record is not None:
            slot = uuid.UUID(self.record["job"]), uuid.UUID(self.record["reservation"])
            allowed = slot == (job_id, reservation_token) or (
                slot == startup_identity(self.journal.guard.device_uuid)
                and self.record["purpose"] in {"maintenance", "recovery"}
            )
            if not allowed:
                raise gpu_resources.DeviceOwnershipError(
                    "Container record does not match the reserved task or startup maintenance"
                )

        def check_database():
            with bounded_database():
                gpu_resources.verify_recovery_owner(
                    self.journal.guard, job_id=job_id, reservation_token=reservation_token
                )

        on_tick()
        check_database()
        self.unchanged()
        cleanup = self.journal.recover()
        check_database()
        on_tick()
        return {"had_record": self.record is not None, "cleanup": cleanup, "ready": False}
