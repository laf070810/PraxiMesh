"""Persist the database decision before opening admission or the GPU Web route.

This internal flow never clones databases, restarts workers or clears user work.
Public GPU apply/recovery remains gated until the whole lifecycle is connected.
"""

import hashlib
import json
import time
import uuid
from datetime import datetime

from judge.container_deployment_config import digest
from scripts.deploy_gpu_candidate import GPUGenerationPreparation
from scripts.deploy_gpu_candidate import validate_record as validate_preparation
from scripts.deploy_gpu_check import OBSERVATION
from scripts.deploy_gpu_config import CONTEXT, frozen
from scripts.deploy_gpu_worker import HEX, started

PHASES = {"decided", "resumed", "activating", "active"}


class GPUExposureError(RuntimeError):
    pass


def record_key(record):
    return ":".join((record["operation"], record["generation"]["gpu_worker"], record["round_id"]))


def validate_record(record):
    try:
        valid = (
            type(record) is dict
            and set(record)
            == {
                "schema",
                "operation",
                "generation",
                "release",
                "round_id",
                "observation",
                "port",
                "phase",
                "completion",
            }
            and type(record["schema"]) is int
            and record["schema"] == 1
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and str(uuid.UUID(record["round_id"])) == record["round_id"]
            and set(record["generation"]) == CONTEXT | {"web", "worker"}
            and type(record["release"]) is str
            and record["release"].startswith("/")
            and type(record["port"]) is int
            and 1 <= record["port"] <= 65535
            and record["phase"] in PHASES
            and record["completion"] in {"active", "rolled_back"}
        )
        observed = record["observation"]
        valid = valid and set(observed) == OBSERVATION
        valid = valid and all(
            HEX.fullmatch(observed[key])
            for key in ("container", "fingerprint", "configuration_sha256")
        )
        valid = valid and str(uuid.UUID(observed["owner_token"])) == observed["owner_token"]
        valid = valid and datetime.fromisoformat(observed["checked_at"]).utcoffset() is not None
        started(observed["started_at"])
    except (KeyError, TypeError, ValueError, AttributeError, RuntimeError):
        valid = False
    if not valid:
        raise GPUExposureError("Invalid original GPU exposure decision")


class GPUExposure:
    def __init__(self, host, generation, release, *, phase="active"):
        if phase not in {"active", "rolled_back"}:
            raise GPUExposureError("Unknown GPU exposure completion phase")
        self.host, self.generation, self.completion = host, frozen(generation), phase
        self.preparation = GPUGenerationPreparation(host, self.generation, release)
        self.readiness = self.preparation.check.readiness
        self.record = None

    def prepared(self):
        """Inspect only; never create a preparation while trying to expose."""
        self.host.require_locked()
        self.host.check_gpu_attachment()
        operation = self.host.state.get("operation") or {}
        token = operation.get("id")
        try:
            valid = type(token) is str and str(uuid.UUID(token)) == token
        except ValueError:
            valid = False
        if not valid:
            raise GPUExposureError("GPU exposure requires the original operation")
        key = token + ":" + self.generation["gpu_worker"]
        values = self.host.state.get("gpu_preparations", {})
        if type(values) is not dict or key not in values:
            raise GPUExposureError("GPU exposure requires completed original preparation")
        preparation = values[key]
        validate_preparation(preparation)
        entry = preparation["attempts"][-1]
        prefix = f"px-{self.host.config['instance']}-{self.generation['id']}"
        if (
            preparation["operation"] != token
            or preparation["generation"] != self.generation
            or preparation["release"] != str(self.preparation.release)
            or entry["phase"] != "verified"
            or any(self.generation[k] != prefix + "-" + k for k in ("web", "worker"))
            or self.generation["gpu_worker"] != prefix + "-gpu-worker"
            or not any(
                value == self.generation
                for value in (
                    self.host.state.get("current"),
                    operation.get("candidate"),
                    operation.get("previous"),
                )
            )
        ):
            raise GPUExposureError("GPU exposure preparation identity changed")
        self.preparation.record = preparation
        self.preparation.verified_round()
        return token, entry

    def original(self):
        token, entry = self.prepared()
        operation = self.host.state["operation"]
        if (
            operation.get("exposure_target") != self.generation["id"]
            or self.host.state.get("current") != self.generation
            or self.host.state.get("gpu_exposures", {}).get(self.key) != self.record
            or self.record["operation"] != token
            or self.record["round_id"] != entry["round_id"]
            or any(
                self.record["observation"][k] != entry["observation"][k]
                for k in OBSERVATION - {"checked_at"}
            )
        ):
            raise GPUExposureError("GPU exposure cannot replace its original database/startup")
        if self.host.port(self.generation) != self.record["port"]:
            raise GPUExposureError("Original exposure Web port changed")
        port = self.host.proxy.probe()["port"]
        if port not in (None, self.record["port"]):
            raise GPUExposureError("GPU exposure found an unrelated live route")
        return port

    def load(self):
        token, entry = self.prepared()
        values = self.host.state.get("gpu_exposures", {})
        if type(values) is not dict:
            raise GPUExposureError("Invalid GPU exposure registry")
        previous = []
        for key, value in values.items():
            validate_record(value)
            if key != record_key(value):
                raise GPUExposureError("Original GPU exposure key changed")
            if value["operation"] == token:
                if (
                    value["generation"] != self.generation
                    or value["release"] != str(self.preparation.release)
                    or value["completion"] != self.completion
                ):
                    raise GPUExposureError("GPU exposure cannot change its decided generation")
                previous.append(value)
        self.key = ":".join((token, self.generation["gpu_worker"], entry["round_id"]))
        record = values.get(self.key)
        if record is None:
            exposed = self.host.state["operation"].get("exposure_target")
            if (
                previous
                and not exposed
                or exposed
                and (
                    not previous
                    or exposed != self.generation["id"]
                    or self.host.state.get("current") != self.generation
                )
            ):
                raise GPUExposureError("Original exposure decision is missing or inconsistent")
            # This call is legal only under the original pause/maintenance route.
            # It re-observes both workers; a saved verified phase is insufficient.
            prepared = self.preparation.run()
            record = {
                "schema": 1,
                "operation": token,
                "generation": self.generation,
                "release": str(self.preparation.release),
                "round_id": entry["round_id"],
                "observation": prepared["gpu"],
                "port": self.host.port(self.generation),
                "phase": "decided",
                "completion": self.completion,
            }
            validate_record(record)
            self.record = frozen(record)
            self.host.state["current"] = frozen(self.generation)
            self.host.state["operation"]["exposure_target"] = self.generation["id"]
            # The decision and current DB are one atomic write, BEFORE resume.
            self.save("decided")
        else:
            self.record = frozen(record)
        self.original()

    def save(self, phase):
        self.record["phase"] = phase
        validate_record(self.record)
        self.host.state.setdefault("gpu_exposures", {})[self.key] = frozen(self.record)
        self.host.save(self.completion if phase == "active" else "exposing")

    def instance(self):
        self.original()
        runtime = self.readiness.runtime
        value = self.readiness.running_instance(*runtime.configuration())
        observed = self.record["observation"]
        if (value[3], value[4], digest(value[1])) != (
            observed["container"],
            observed["started_at"],
            observed["configuration_sha256"],
        ):
            raise GPUExposureError("GPU exposure worker/configuration changed")
        return value

    def snapshot(self, instance, *, opened=None):
        mark = time.monotonic()
        value = self.host.database.gpu_exposure_snapshot(
            self.generation["database"], instance[2], instance[0]["gpu"]
        )
        admission = value["admission"]
        if admission not in {"paused", "open"} or (
            opened is not None and (admission == "open") != opened
        ):
            raise GPUExposureError("GPU exposure admission changed")
        if admission == "paused":
            valid = self.original() is None and self.readiness.valid_snapshot(
                value, instance, time.monotonic() - mark
            )
        else:
            valid = self.readiness.valid_device(value, instance, time.monotonic() - mark)
            device = value["device"]
            if valid and device["state"] == "ready":
                valid = device["job_id"] is None and device["reservation_token"] is None
            elif valid and device["state"] == "reserved":
                try:
                    job = str(device["job_id"])
                    reservation = str(device["reservation_token"])
                    valid = (
                        str(uuid.UUID(job)) == job and str(uuid.UUID(reservation)) == reservation
                    )
                except ValueError:
                    valid = False
            else:
                valid = False
        device = value["device"]
        expected = self.record["observation"]
        if not valid or (
            str(device["owner_token"]) != expected["owner_token"]
            or device["fingerprint"] != expected["fingerprint"]
        ):
            raise GPUExposureError("GPU exposure lost fresh original device evidence")
        return value, mark

    def admission(self, value):
        """Reuse frozen Web's full lease/budget/policy admission, including busy GPUs."""
        raw = self.host.control(self.generation, "judge_admission", "status", timeout=30)
        try:
            if type(raw) is not bytes or len(raw) > 16384:
                raise ValueError("Invalid status response")
            status = json.loads(raw)
            judge = self.generation["manifest"]["judge"]
            fingerprint = hashlib.sha256(
                f"wasi:{judge['bundle_sha256']}:{judge['policy_sha256']}".encode()
            ).hexdigest()
            paused = value["admission"] == "paused"
            valid = (
                type(status) is dict
                and status["admission"] == value["admission"]
                and status["token"] == (self.record["operation"] if paused else None)
                and (
                    datetime.fromisoformat(status["paused_at"]).utcoffset() is not None
                    if paused
                    else status["paused_at"] is None
                )
                and status["fingerprint"] == fingerprint
                and all(
                    status[k] is True
                    for k in (
                        "worker_available",
                        "gpu_configured",
                        "gpu_worker_available",
                    )
                )
                and all(
                    type(status[k]) is int and status[k] == 0
                    for k in (
                        "incompatible_pending",
                        "incompatible_gpu_reservations",
                    )
                )
                and all(
                    type(status[k]) is int and status[k] >= 0
                    for k in (
                        "queued",
                        "running",
                        "gpu_reserved",
                    )
                )
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            valid = False
        if not valid:
            raise GPUExposureError("Frozen application has not confirmed CPU/GPU admission")

    def observe(self, *, opened=None):
        initial = self.instance()
        value, _ = self.snapshot(initial, opened=opened)
        self.admission(value)
        if not self.host.ready(self.generation) or self.instance() != initial:
            raise GPUExposureError("Original CPU/GPU worker lost readiness during exposure")
        final, mark = self.snapshot(initial, opened=value["admission"] == "open")
        if (
            self.instance() != initial
            or not self.readiness.valid_device(
                final, initial, time.monotonic() - mark, verify_environment=False
            )
            or any(
                final["device"][k] != value["device"][k]
                for k in ("owner_token", "fingerprint", "environment")
            )
        ):
            raise GPUExposureError("Original GPU identity changed during exposure observation")
        return final["admission"]

    def run(self):
        self.load()
        if self.observe() == "paused":
            if self.record["phase"] != "decided":
                raise GPUExposureError("Admission was paused again; recover the original database")
            self.host.control(
                self.generation, "judge_admission", "resume", "--token", self.record["operation"]
            )
        self.observe(opened=True)
        if self.record["phase"] == "decided":
            self.save("resumed")
        if self.record["phase"] == "resumed":
            self.save("activating")
        if self.original() is None:
            if self.record["phase"] == "active":
                raise GPUExposureError("Active route was withdrawn; recover the original database")
            self.host.proxy.activate(self.record["port"])
        self.host.health(self.generation, external=True, require_gpu=True)
        self.observe(opened=True)
        if self.original() != self.record["port"]:
            raise GPUExposureError("Original GPU route was not confirmed")
        if self.record["phase"] != "active":
            self.save("active")
        return {
            "generation": self.generation["id"],
            "database": self.generation["database"],
            "round_id": self.record["round_id"],
            "phase": self.completion,
        }
