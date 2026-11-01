"""Journal candidate queue checks on the original deployment database.

This is an internal component, not a GPU activation or readiness bypass.
"""

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path

from judge.container_deployment_config import canonical, digest
from scripts.deploy_gpu_config import context
from scripts.deploy_gpu_readiness import GPUReadiness, literal
from scripts.deploy_gpu_worker import HEX, started
from scripts.release import verify_source

PROTOCOL = "gpu-deployment-check-v1"
SOURCES = ("core/deployment_gpu_check.py", "core/management/commands/deployment_gpu_check.py")
PHASES = {"checking", "checked", "cleaning", "verified", "cleaned"}
OBSERVATION = {
    "container",
    "started_at",
    "configuration_sha256",
    "owner_token",
    "fingerprint",
    "checked_at",
}
FIELDS = {
    "schema",
    "operation",
    "generation",
    "cpu_worker",
    "device",
    "runtime_sha256",
    "phase",
    "observation",
    "cleanup_observation",
    "report",
    "cleanup_report",
}


class GPUCandidateError(RuntimeError):
    pass


def expected_report(record, cleanup=False):
    observed = record["cleanup_observation"] if cleanup else record["observation"]
    worker = record["generation"]["gpu_worker"]
    identity = uuid.uuid5(
        uuid.NAMESPACE_URL, f"{PROTOCOL}:{record['operation']}:{worker}:{observed['fingerprint']}"
    )
    return {
        "protocol": PROTOCOL,
        "action": "cleanup" if cleanup else "check",
        "passed": not cleanup,
        "token": record["operation"],
        "worker_id": worker,
        "cpu_worker_id": record["cpu_worker"],
        "device": record["device"],
        "owner_token": observed["owner_token"],
        "fingerprint": observed["fingerprint"],
        "started_at": datetime.fromisoformat(
            observed["started_at"].replace("Z", "+00:00")
        ).isoformat(),
        "fixture": "gpu-deploy-check-" + identity.hex,
        "verdicts": [] if cleanup else ["accepted", "wrong_answer"],
        "admission": "paused",
        "cleanup_passed": cleanup,
    }


def validate_record(record):
    try:
        valid = (
            type(record) is dict
            and set(record) == FIELDS
            and type(record["schema"]) is int
            and record["schema"] == 1
            and record["phase"] in PHASES
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and record["generation"] == context(record["generation"])
            and record["cpu_worker"]
            == record["generation"]["gpu_worker"].removesuffix("-gpu-worker") + "-worker"
            and record["device"] == "GPU-" + str(uuid.UUID(record["device"].removeprefix("GPU-")))
            and HEX.fullmatch(record["runtime_sha256"])
        )
        for observed in (record["observation"], record["cleanup_observation"]):
            if observed is None:
                continue
            valid = valid and type(observed) is dict and set(observed) == OBSERVATION
            valid = valid and all(
                HEX.fullmatch(observed[key])
                for key in ("container", "configuration_sha256", "fingerprint")
            )
            valid = valid and str(uuid.UUID(observed["owner_token"])) == observed["owner_token"]
            valid = valid and bool(started(observed["started_at"]))
            stamp = datetime.fromisoformat(observed["checked_at"])
            valid = valid and stamp.utcoffset() is not None
        valid = valid and record["observation"] is not None
        if record["cleanup_observation"] is not None:
            valid = valid and all(
                record["cleanup_observation"][key] == record["observation"][key]
                for key in ("container", "configuration_sha256", "fingerprint")
            )
        if record["report"] is not None:
            valid = valid and canonical(record["report"]) == canonical(expected_report(record))
        if record["phase"] in {"checked", "verified"}:
            valid = valid and record["report"] is not None
            valid = valid and record["cleanup_observation"] == record["observation"]
        if record["phase"] == "checking":
            valid = valid and record["report"] is None and record["cleanup_observation"] is None
        else:
            valid = valid and record["cleanup_observation"] is not None
        if record["phase"] in {"verified", "cleaned"}:
            valid = valid and canonical(record["cleanup_report"]) == canonical(
                expected_report(record, True)
            )
        else:
            valid = valid and record["cleanup_report"] is None
    except (KeyError, TypeError, ValueError, AttributeError, RuntimeError):
        valid = False
    if not valid:
        raise GPUCandidateError("Invalid original GPU candidate verification record")


class GPUCandidateCheck:
    def __init__(self, host, generation, release):
        self.host = host
        self.generation = copy.deepcopy(generation)
        self.readiness = GPUReadiness(host, generation, release)
        manifest = generation["manifest"]
        if not set(SOURCES) <= set(manifest["gpu"]["source_scopes"]["worker"]):
            raise GPUCandidateError("Frozen release does not support the GPU candidate protocol")
        contents = verify_source(
            Path(release) / "source.tar.gz", manifest["source_files"], capture=SOURCES
        )
        if literal(contents, SOURCES[0], "PROTOCOL") != PROTOCOL:
            raise GPUCandidateError("Frozen GPU candidate protocol is unsupported")

    def save(self, phase):
        self.record["phase"] = phase
        self.host.state.setdefault("gpu_candidate_checks", {})[self.key] = self.record
        self.host.save()

    def instance(self, observed):
        config, metadata, token, container, stamp = self.readiness.instance()
        if (
            token != self.record["operation"]
            or config["gpu"] != self.record["device"]
            or digest(metadata) != observed["configuration_sha256"]
            or container != observed["container"]
            or stamp != observed["started_at"]
        ):
            raise GPUCandidateError("Original GPU candidate startup changed")

    def fresh(self, observed):
        current = self.readiness.observe()
        if current is None or any(
            current[key] != observed[key] for key in OBSERVATION - {"checked_at"}
        ):
            raise GPUCandidateError("GPU candidate lost its original fresh readiness")
        return current

    def load(self, cleanup):
        config, _, token, _, _ = self.readiness.instance()
        generation = self.readiness.runtime.generation
        operation = self.host.state["operation"]
        if not any(
            value and "gpu_worker" in value and context(value) == generation
            for value in (
                self.host.state.get("current"),
                operation.get("previous"),
                operation.get("candidate"),
            )
        ):
            raise GPUCandidateError("GPU candidate is outside the recorded deployment operation")
        self.key = token + ":" + generation["gpu_worker"]
        records = self.host.state.get("gpu_candidate_checks", {})
        if type(records) is not dict:
            raise GPUCandidateError("Invalid GPU candidate journal")
        for key, record in records.items():
            validate_record(record)
            if key != record["operation"] + ":" + record["generation"]["gpu_worker"]:
                raise GPUCandidateError("GPU candidate journal key changed")
            if (
                key != self.key
                and record["generation"] == generation
                and record["phase"] not in {"verified", "cleaned"}
            ):
                raise GPUCandidateError("Finish the original GPU candidate operation first")
        self.record = records.get(self.key)
        runtime = self.readiness.runtime.record
        cpu_worker = f"px-{self.host.config['instance']}-{generation['id']}-worker"
        if self.generation.get("worker", cpu_worker) != cpu_worker:
            raise GPUCandidateError("GPU candidate CPU worker differs from the original generation")
        if self.record is None:
            if cleanup:
                raise GPUCandidateError("No original GPU candidate intent to clean")
            observed = self.readiness.wait()
            self.record = {
                "schema": 1,
                "operation": token,
                "generation": generation,
                "cpu_worker": cpu_worker,
                "device": config["gpu"],
                "runtime_sha256": runtime["runtime_sha256"],
                "phase": "checking",
                "observation": observed,
                "cleanup_observation": None,
                "report": None,
                "cleanup_report": None,
            }
            self.save("checking")
        if (
            self.record["generation"] != generation
            or self.record["device"] != config["gpu"]
            or self.record["cpu_worker"] != cpu_worker
            or self.record["runtime_sha256"] != runtime["runtime_sha256"]
        ):
            raise GPUCandidateError("GPU candidate generation or runtime identity changed")

    def run(self, *, cleanup=False):
        self.load(cleanup)
        if not cleanup and self.record["phase"] in {"cleaning", "cleaned"}:
            raise GPUCandidateError("Cleaned GPU candidate intent cannot authorize verification")
        if cleanup:
            if self.record["phase"] == "cleaned":
                self.instance(self.record["cleanup_observation"])
                self.fresh(self.record["cleanup_observation"])
                return copy.deepcopy(self.record["cleanup_report"])
            observed = self.readiness.wait()
            original = self.record["observation"]
            if any(
                observed[key] != original[key]
                for key in ("container", "configuration_sha256", "fingerprint")
            ):
                raise GPUCandidateError(
                    "Cleanup requires the original configuration and environment"
                )
            self.record.update(cleanup_observation=observed, cleanup_report=None)
            self.save("cleaning")
            self.record["cleanup_report"] = self.command(cleanup=True)
            self.save("cleaned")
            return copy.deepcopy(self.record["cleanup_report"])
        else:
            observed = self.record["observation"]
            self.instance(observed)
            if self.record["phase"] == "verified":
                self.fresh(observed)
                return copy.deepcopy(self.record["report"] | {"cleanup_passed": True})
        if self.record["phase"] == "checking":
            self.record["report"] = self.command()
            self.record["cleanup_observation"] = copy.deepcopy(observed)
            # Keep the original submissions until their evidence is durably acknowledged.
            self.save("checked")
        self.record["cleanup_report"] = self.command(cleanup=True)
        self.save("verified")
        return copy.deepcopy(self.record["report"] | {"cleanup_passed": True})

    def command(self, *, cleanup=False):
        observed = self.record["cleanup_observation"] if cleanup else self.record["observation"]
        self.instance(observed)
        command = ["deployment_gpu_check", "cleanup" if cleanup else "check"]
        for key, value in {
            "token": self.record["operation"],
            "worker-id": self.record["generation"]["gpu_worker"],
            "cpu-worker-id": self.record["cpu_worker"],
            "device": self.record["device"],
            "owner-token": observed["owner_token"],
            "fingerprint": observed["fingerprint"],
            "started-at": observed["started_at"],
        }.items():
            command.extend(("--" + key, value))
        raw = self.host.control(self.generation, *command, timeout=540)
        try:
            if len(raw) > 16384:
                raise ValueError
            result = json.loads(raw)
            if canonical(result) != canonical(expected_report(self.record, cleanup)):
                raise ValueError
        except (TypeError, ValueError):
            raise GPUCandidateError(
                "GPU candidate command returned incomplete or mismatched evidence"
            ) from None
        self.instance(observed)
        self.fresh(observed)
        return result
