"""Durable recovery on the currently configured GPU database; no exposure switch.

No task journal/reservation is removed by this coordinator. The original fixed
worker must recover it through complete startup before readiness can pass.
"""

import copy
import uuid

from judge.container_deployment_config import digest
from scripts.deploy_gpu_config import context
from scripts.deploy_gpu_readiness import GPUReadiness, GPUReadinessError
from scripts.deploy_gpu_worker import (
    HEX,
    WorkerLifecycleError,
    WorkerRuntime,
    running,
    started,
    stopped,
)

PHASES = {"stopping", "resetting_check", "checking", "starting", "waiting", "ready"}
FIELDS = {
    "schema",
    "operation",
    "generation",
    "metadata_sha256",
    "container",
    "runtime_sha256",
    "phase",
    "attempts",
}
ATTEMPT_FIELDS = {"before", "started", "check_id", "check_before", "check_started", "failed_checks"}
MAX_ATTEMPTS = 16


class GPURecoveryError(RuntimeError):
    pass


def attempt(before):
    return {
        "before": before,
        "started": None,
        "check_id": None,
        "check_before": None,
        "check_started": None,
        "failed_checks": [],
    }


def validate_record(record):
    """Pure journal validation, also used before any configuration publication."""
    try:
        valid = (
            type(record) is dict
            and set(record) == FIELDS
            and type(record["schema"]) is int
            and record["schema"] == 1
            and record["phase"] in PHASES
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and context(record["generation"]) == record["generation"]
            and all(
                HEX.fullmatch(record[key])
                for key in ("container", "runtime_sha256", "metadata_sha256")
            )
            and type(record["attempts"]) is list
            and 1 <= len(record["attempts"]) <= MAX_ATTEMPTS
        )
        for entry in record["attempts"]:
            valid = valid and type(entry) is dict and set(entry) == ATTEMPT_FIELDS
            started(entry["before"])
            for key in ("started", "check_before", "check_started"):
                if entry[key] is not None:
                    started(entry[key])
            valid = valid and (entry["check_id"] is None or HEX.fullmatch(entry["check_id"]))
            valid = (
                valid
                and type(entry["failed_checks"]) is list
                and len(entry["failed_checks"]) <= MAX_ATTEMPTS
            )
            for stamp in entry["failed_checks"]:
                started(stamp)
        last = record["attempts"][-1]
        if record["phase"] in {"checking", "starting", "waiting", "ready"}:
            valid = valid and last["check_id"] is not None and last["check_before"] is not None
        if record["phase"] in {"starting", "waiting", "ready"}:
            valid = valid and last["check_started"] is not None
        if record["phase"] in {"waiting", "ready"}:
            valid = valid and last["started"] is not None and last["started"] != last["before"]
    except (KeyError, TypeError, ValueError, AttributeError, WorkerLifecycleError):
        valid = False
    if not valid:
        raise GPURecoveryError("Invalid original GPU recovery attempt")


class GPUWorkerRecovery:
    def __init__(self, host, generation, release):
        self.host = host
        self.readiness = GPUReadiness(host, generation, release)
        self.worker = self.readiness.runtime
        self.check = WorkerRuntime(host, generation, mode="check")
        self.record = None

    def original(self):
        config, metadata, token = self.worker.common()
        operation = self.host.state["operation"]
        exposed = operation.get("exposure_target")
        current = self.host.state.get("current")
        if exposed and (
            exposed != self.worker.generation["id"]
            or not current
            or context(current) != self.worker.generation
        ):
            raise GPURecoveryError("Recovery cannot return to a database before exposure")
        generations = (current, operation.get("previous"), operation.get("candidate"))
        if not any(
            value and "gpu_worker" in value and context(value) == self.worker.generation
            for value in generations
        ):
            raise GPURecoveryError("Recovery requires the recorded original GPU generation")
        runtime = self.worker.load()
        if not config["enabled"] or runtime is None or runtime["id"] is None:
            raise GPURecoveryError("Finish original worker creation/configuration before recovery")
        info = self.worker.inspect()
        if self.record is not None and (
            self.record["operation"] != token
            or self.record["generation"] != self.worker.generation
            or self.record["metadata_sha256"] != digest(metadata)
            or self.record["container"] != runtime["id"]
            or self.record["runtime_sha256"] != runtime["runtime_sha256"]
        ):
            raise GPURecoveryError("Original GPU recovery identity changed")
        return metadata, token, runtime, info

    def save(self, phase):
        self.record["phase"] = phase
        self.host.state.setdefault("gpu_recoveries", {})[self.key] = self.record
        self.host.save()

    def load(self):
        metadata, token, runtime, info = self.original()
        self.key = token + ":" + self.worker.name
        records = self.host.state.get("gpu_recoveries", {})
        if type(records) is not dict:
            raise GPURecoveryError("Invalid GPU recovery journal")
        for key, value in records.items():
            validate_record(value)
            if key != value["operation"] + ":" + value["generation"]["gpu_worker"]:
                raise GPURecoveryError("Original GPU recovery record key changed")
            if (
                value["container"] == runtime["id"]
                and key != self.key
                and value["phase"] != "ready"
            ):
                raise GPURecoveryError("Finish the original GPU recovery operation first")
        record = records.get(self.key)
        if record is not None:
            self.record = record
            self.original()
            return
        self.record = {
            "schema": 1,
            "operation": token,
            "generation": copy.deepcopy(self.worker.generation),
            "metadata_sha256": digest(metadata),
            "container": runtime["id"],
            "runtime_sha256": runtime["runtime_sha256"],
            "phase": "stopping",
            "attempts": [attempt(info["State"]["StartedAt"])],
        }
        self.save("stopping")

    @property
    def attempt(self):
        return self.record["attempts"][-1]

    def stopped_worker(self):
        *_, info = self.original()
        if not stopped(info) or info["State"]["StartedAt"] != self.attempt["before"]:
            raise GPURecoveryError("Original worker must remain stopped during its fresh preflight")

    def check_instance(self):
        record = self.check.load()
        if record is None or record["id"] != self.attempt["check_id"]:
            raise GPURecoveryError("Original recovery preflight identity changed")
        return record, self.check.inspect()

    def retry_exited_attempt(self):
        """Only a later invocation may retry a previously observed failed startup."""
        _, token, runtime, info = self.original()
        phase, entry = self.record["phase"], self.attempt
        observed = info["State"]["StartedAt"]
        if (
            phase in {"starting", "waiting", "ready"}
            and stopped(info)
            and observed != entry["before"]
        ):
            expected = (
                phase == "starting"
                and runtime["launch_before"] == entry["before"]
                or phase in {"waiting", "ready"}
                and observed == entry["started"]
            )
            if (
                expected
                and phase == "starting"
                and runtime["phase"] == "start_requested"
                and runtime["operation"] == token
            ):
                # The daemon may have started and exited before confirmation was saved.
                # Reconcile that intent first; a failed start is retried only next call.
                self.worker.start()
                return
            if not expected or runtime["operation"] != token or runtime["started_at"] != observed:
                raise GPURecoveryError("Stopped worker is not the original recovery startup")
            if len(self.record["attempts"]) >= MAX_ATTEMPTS:
                raise GPURecoveryError("Original recovery attempt limit reached; records retained")
            self.record["attempts"].append(attempt(observed))
            self.save("stopping")
        if self.record["phase"] == "checking":
            check, info = self.check_instance()
            if check["phase"] == "start_failed":
                observed = info["State"]["StartedAt"]
                if (
                    not stopped(info)
                    or check["operation"] != token
                    or check["started_at"] != observed
                    or check["launch_before"] != entry["check_before"]
                    or len(entry["failed_checks"]) >= MAX_ATTEMPTS
                ):
                    raise GPURecoveryError("Original failed preflight cannot be retried")
                entry["failed_checks"].append(observed)
                entry["check_before"], entry["check_started"] = observed, None
                self.save("resetting_check")

    def run(self):
        self.load()
        self.retry_exited_attempt()
        entry = self.attempt
        if self.record["phase"] == "stopping":
            *_, info = self.original()
            if info["State"]["StartedAt"] != entry["before"]:
                raise GPURecoveryError("Original worker changed before recovery stop")
            self.worker.stop()
            self.save("resetting_check")
        if self.record["phase"] == "resetting_check":
            self.stopped_worker()
            info = self.check.create()
            if entry["check_id"] is None:
                entry["check_id"], entry["check_before"] = info["Id"], info["State"]["StartedAt"]
                self.save("resetting_check")
            _, info = self.check_instance()
            if info["State"]["StartedAt"] != entry["check_before"]:
                raise GPURecoveryError("Original preflight changed before recovery reset")
            self.check.stop()
            self.save("checking")
        if self.record["phase"] == "checking":
            self.stopped_worker()
            self.check_instance()
            info = self.check.check()
            if info["State"]["StartedAt"] == entry["check_before"]:
                raise GPURecoveryError("Recovery preflight did not run again")
            entry["check_started"] = info["State"]["StartedAt"]
            self.save("starting")
        if self.record["phase"] == "starting":
            self.original()
            check, info = self.check_instance()
            if (
                check["phase"] != "completed"
                or info["State"]["StartedAt"] != entry["check_started"]
            ):
                raise GPURecoveryError("Original fresh recovery preflight changed")
            info = self.worker.start()
            entry["started"] = info["State"]["StartedAt"]
            self.save("waiting")
        *_, info = self.original()
        if not running(info) or info["State"]["StartedAt"] != entry["started"]:
            raise GPURecoveryError("Original recovery startup instance changed")
        result = (
            self.readiness.wait() if self.record["phase"] == "waiting" else self.readiness.observe()
        )
        if result is None:
            raise GPUReadinessError(
                "Original recovered worker is no longer ready; records retained"
            )
        *_, info = self.original()
        if not running(info) or info["State"]["StartedAt"] != entry["started"]:
            raise GPURecoveryError("Original recovered worker exited before confirmation")
        self.save("ready")
        return result
