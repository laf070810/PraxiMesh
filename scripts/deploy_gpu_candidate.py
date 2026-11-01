"""Durable GPU generation preparation using the original component lifecycles.

No database clone, exposure, admission resume, or public activation bypass.
"""

import uuid
from datetime import datetime
from pathlib import Path

from judge.container_deployment_config import canonical
from scripts.deploy_gpu_check import (
    OBSERVATION,
    GPUCandidateCheck,
    expected_report,
    record_key,
)
from scripts.deploy_gpu_check import (
    validate_record as validate_check,
)
from scripts.deploy_gpu_config import CONTEXT, context, frozen
from scripts.deploy_gpu_recovery import validate_record as validate_recovery
from scripts.deploy_gpu_worker import HEX, started

PHASES = {
    "publishing",
    "starting_services",
    "resetting_preflight",
    "preflighting",
    "creating",
    "starting",
    "restoring_services",
    "waiting",
    "checking_cpu",
    "checking_gpu",
    "verified",
}
EARLY = {
    "publishing",
    "starting_services",
    "resetting_preflight",
    "preflighting",
    "creating",
    "starting",
}
OBSERVED = {"checking_cpu", "checking_gpu", "verified"}
MAX_ATTEMPTS = 16


class GPUPreparationError(RuntimeError):
    pass


def attempt():
    return {
        "phase": "publishing",
        "startup": None,
        "observation": None,
        "round_id": None,
        "recovered": False,
    }


def validate_record(record):
    try:
        valid = (
            type(record) is dict
            and set(record) == {"schema", "operation", "generation", "release", "attempts"}
            and type(record["schema"]) is int
            and record["schema"] == 1
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and set(record["generation"]) == CONTEXT | {"web", "worker"}
            and type(record["release"]) is str
            and Path(record["release"]).is_absolute()
            and type(record["attempts"]) is list
            and 1 <= len(record["attempts"]) <= MAX_ATTEMPTS
        )
        for entry in record["attempts"]:
            valid = valid and type(entry) is dict and set(entry) == set(attempt())
            valid = valid and entry["phase"] in PHASES and type(entry["recovered"]) is bool
            if entry["phase"] in EARLY:
                valid = valid and entry["startup"] is None and not entry["recovered"]
            else:
                valid = valid and set(entry["startup"]) == {"container", "started_at"}
                valid = valid and bool(HEX.fullmatch(entry["startup"]["container"]))
                started(entry["startup"]["started_at"])
            if entry["phase"] in OBSERVED:
                observed = entry["observation"]
                valid = valid and set(observed) == OBSERVATION
                valid = valid and all(observed[k] == v for k, v in entry["startup"].items())
                valid = valid and all(
                    HEX.fullmatch(observed[k])
                    for k in ("container", "fingerprint", "configuration_sha256")
                )
                valid = valid and str(uuid.UUID(observed["owner_token"])) == observed["owner_token"]
                valid = (
                    valid and datetime.fromisoformat(observed["checked_at"]).utcoffset() is not None
                )
            else:
                valid = valid and entry["observation"] is None
            if entry["phase"] == "verified":
                valid = valid and str(uuid.UUID(entry["round_id"])) == entry["round_id"]
            else:
                valid = valid and entry["round_id"] is None
    except (KeyError, TypeError, ValueError, AttributeError, RuntimeError):
        valid = False
    if not valid:
        raise GPUPreparationError("Invalid original GPU preparation journal")


class GPUGenerationPreparation:
    def __init__(self, host, generation, release):
        self.host, self.generation = host, frozen(generation)
        self.release = Path(release).absolute()
        if self.release.resolve() != self.release or not self.release.is_dir():
            raise GPUPreparationError(
                "GPU preparation requires the original canonical archive path"
            )
        # Validate the entire frozen release and v2 capability before mutations.
        self.check = GPUCandidateCheck(host, self.generation, self.release)
        self.runtime = self.check.readiness.runtime
        self.record = None

    def original(self):
        self.host.require_locked()
        self.host.check_gpu_attachment()
        operation = self.host.state.get("operation") or {}
        token = operation.get("id")
        try:
            valid = type(token) is str and str(uuid.UUID(token)) == token
        except ValueError:
            valid = False
        prefix = f"px-{self.host.config['instance']}-{self.generation['id']}"
        if (
            not valid
            or set(self.generation) != CONTEXT | {"web", "worker"}
            or any(self.generation[role] != prefix + "-" + role for role in ("web", "worker"))
            or self.generation["gpu_worker"] != prefix + "-gpu-worker"
            or not any(
                value == self.generation
                for value in (
                    self.host.state.get("current"),
                    operation.get("previous"),
                    operation.get("candidate"),
                )
            )
        ):
            raise GPUPreparationError("Preparation requires the complete original GPU generation")
        exposed = operation.get("exposure_target")
        if exposed and (
            exposed != self.generation["id"] or self.host.state.get("current") != self.generation
        ):
            raise GPUPreparationError("Preparation cannot return to a pre-exposure database")
        if self.host.proxy.probe()["port"] is not None:
            raise GPUPreparationError("GPU preparation requires the actual maintenance route")
        self.host.database.gpu_worker_pause(self.generation["database"], token)
        if self.record is not None and (
            self.record["operation"] != token
            or self.record["generation"] != self.generation
            or self.record["release"] != str(self.release)
        ):
            raise GPUPreparationError("Original GPU preparation identity changed")
        return token

    @property
    def entry(self):
        return self.record["attempts"][-1]

    def save(self, phase):
        self.entry["phase"] = phase
        validate_record(self.record)
        self.host.state.setdefault("gpu_preparations", {})[self.key] = self.record
        self.host.save()

    def load(self):
        token = self.original()
        self.key = token + ":" + self.generation["gpu_worker"]
        records = self.host.state.get("gpu_preparations", {})
        if type(records) is not dict:
            raise GPUPreparationError("Invalid GPU preparation registry")
        for key, value in records.items():
            validate_record(value)
            if key != value["operation"] + ":" + value["generation"]["gpu_worker"]:
                raise GPUPreparationError("Original GPU preparation key changed")
            if (
                key != self.key
                and value["generation"] == self.generation
                and value["attempts"][-1]["phase"] != "verified"
            ):
                raise GPUPreparationError("Finish the original GPU preparation operation first")
        self.record = records.get(self.key)
        if self.record is None:
            self.record = {
                "schema": 1,
                "operation": token,
                "generation": self.generation,
                "release": str(self.release),
                "attempts": [attempt()],
            }
            self.save("publishing")
        self.original()

    def fresh(self, *, wait=False):
        self.original()
        observed = self.host.gpu_worker_ready(self.generation, self.release, wait=wait)
        if observed is None or any(
            observed[key] != value for key, value in self.entry["startup"].items()
        ):
            raise GPUPreparationError("Prepared GPU startup is no longer ready")
        previous = self.entry["observation"]
        if previous is not None and any(
            observed[k] != previous[k] for k in OBSERVATION - {"checked_at"}
        ):
            raise GPUPreparationError("Prepared GPU owner or environment changed")
        return observed

    def verified_round(self, round_id=None):
        records = self.host.state.get("gpu_candidate_checks", {})
        selected = self.entry["round_id"] if round_id is None else round_id
        matching = [record for record in records.values() if record.get("round_id") == selected]
        if len(matching) != 1:
            raise GPUPreparationError("Prepared GPU verification round is missing or ambiguous")
        record = matching[0]
        validate_check(record)
        runtime = self.runtime.load()
        if (
            records.get(record_key(record)) != record
            or record["phase"] != "verified"
            or record["operation"] != self.record["operation"]
            or record["generation"] != context(self.generation)
            or record["device"] != self.host.config["gpu"]["device"]
            or runtime is None
            or record["runtime_sha256"] != runtime["runtime_sha256"]
            or any(
                record["observation"][k] != self.entry["observation"][k]
                for k in OBSERVATION - {"checked_at"}
            )
        ):
            raise GPUPreparationError("Prepared GPU verification belongs to another startup")
        return expected_report(record) | {"cleanup_passed": True}

    def run(self):
        self.load()
        if self.entry["phase"] == "publishing":
            self.host.publish_gpu_configuration(self.generation, enabled=True)
            self.save("starting_services")
        if self.entry["phase"] == "starting_services":
            self.original()
            self.host.service(self.generation, "web")
            self.host.service(self.generation, "worker")
            self.save("resetting_preflight")
        if self.entry["phase"] == "resetting_preflight":
            self.host.gpu_worker_runtime(self.generation, "create", mode="check")
            self.host.gpu_worker_runtime(self.generation, "stop", mode="check")
            self.save("preflighting")
        if self.entry["phase"] == "preflighting":
            self.host.gpu_worker_runtime(self.generation, "check", mode="check")
            self.save("creating")
        if self.entry["phase"] == "creating":
            self.host.gpu_worker_runtime(self.generation, "create")
            self.save("starting")
        if self.entry["phase"] == "starting":
            info = self.host.gpu_worker_runtime(self.generation, "start")
            self.entry["startup"] = {
                "container": info["Id"],
                "started_at": info["State"]["StartedAt"],
            }
            self.save("waiting")
        if self.entry["phase"] == "restoring_services":
            self.fresh()
            self.host.stop(self.generation)
            self.host.replace_web(self.generation)
            self.host.service(self.generation, "worker")
            self.save("waiting")
        if self.entry["phase"] == "waiting":
            self.entry["observation"] = self.fresh(wait=True)
            self.save("checking_cpu")
        if self.entry["phase"] == "checking_cpu":
            self.fresh()
            self.host.check(self.generation, self.record["operation"])
            self.fresh()
            self.save("checking_gpu")
        if self.entry["phase"] == "checking_gpu":
            self.fresh()
            report = self.host.check_gpu_candidate(self.generation, self.release)
            if type(report) is not dict or canonical(report) != canonical(
                self.verified_round(report.get("round_id"))
            ):
                raise GPUPreparationError(
                    "GPU preparation received mismatched verification evidence"
                )
            self.fresh()
            self.entry["round_id"] = report["round_id"]
            self.save("verified")
        observed = self.fresh()
        if not self.host.ready(self.generation):
            raise GPUPreparationError("Prepared CPU worker is no longer ready")
        return {"gpu": observed, "verification": self.verified_round()}

    def recover(self):
        self.load()
        if len(self.record["attempts"]) >= MAX_ATTEMPTS:
            self.fresh()
            return self.run()
        if self.entry["phase"] == "publishing":
            self.host.publish_gpu_configuration(self.generation, enabled=True)
            self.save("starting_services")
        # This creates only a missing, intended original worker; an existing
        # registered ID is inspected, never replaced or adopted by name.
        self.host.gpu_worker_runtime(self.generation, "create")
        # Original CPU work may also need to finish before GPU readiness can
        # observe a drained queue. Keep it on the same paused database.
        self.host.service(self.generation, "worker")
        observed = self.host.recover_gpu_worker(self.generation, self.release)
        record = self.host.state["gpu_recoveries"][self.key]
        validate_recovery(record)
        if (
            record["phase"] != "ready"
            or record["operation"] != self.record["operation"]
            or record["generation"] != context(self.generation)
            or record["container"] != observed["container"]
            or record["attempts"][-1]["started"] != observed["started_at"]
            or record["metadata_sha256"] != observed["configuration_sha256"]
        ):
            raise GPUPreparationError("Original GPU recovery has not confirmed this startup")
        for value in list(self.host.state.get("gpu_candidate_checks", {}).values()):
            validate_check(value)
            if (
                value["operation"] == self.record["operation"]
                and value["generation"] == context(self.generation)
                and value["phase"] not in {"verified", "cleaned"}
            ):
                self.host.check_gpu_candidate(
                    self.generation, self.release, cleanup=True, round_id=value["round_id"]
                )
        startup = {key: observed[key] for key in ("container", "started_at")}
        if self.entry["startup"] != startup:
            if len(self.record["attempts"]) >= MAX_ATTEMPTS:
                raise GPUPreparationError("GPU preparation attempt limit reached; history retained")
            self.record["attempts"].append(attempt() | {"startup": startup, "recovered": True})
            self.save("restoring_services")
        return self.run()
