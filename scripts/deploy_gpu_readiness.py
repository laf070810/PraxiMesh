"""Observe full-startup publication; never execute devices or publish readiness.

This internal deployment gate is intentionally separate from apply/exposure.
Daemon and database observations cannot replace the worker's private receipt.
"""

import ast
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from judge.container_deployment_config import digest
from judge.container_environment import engine_identity, validate_environment
from judge.driver_artifacts import manifest_digest
from judge.gpu_receipt import SUITE_SECONDS
from scripts.deploy_gpu import read_private
from scripts.deploy_gpu_worker import WorkerRuntime, running
from scripts.release import verify, verify_source

# Same bounded heartbeat window as Web admission; no reservation or device lock.
FRESH_SECONDS = 30
STARTUP_SECONDS = SUITE_SECONDS + 120


class GPUReadinessError(RuntimeError):
    pass


def literal(contents, filename, name):
    """Only one top-level literal assignment; never evaluate archived code."""
    tree = ast.parse(contents[filename])
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if len(values) != 1:
        raise GPUReadinessError("Frozen GPU runtime declaration is missing or ambiguous")
    try:
        return ast.literal_eval(values[0])
    except (TypeError, ValueError):
        raise GPUReadinessError("Frozen GPU runtime declaration is not a literal") from None


def source_digest(contents, names):
    if (
        type(names) not in (list, tuple)
        or not 1 <= len(names) <= 2000
        or any(type(name) is not str or name not in contents for name in names)
        or len(set(names)) != len(names)
    ):
        raise GPUReadinessError("Frozen GPU runtime source scope is incomplete")
    value = hashlib.sha256()
    for name in names:
        value.update(name.encode() + b"\0" + contents[name])
    return value.hexdigest()


def archived_artifacts(manifest, contents):
    """Derive all static artifact fields from verified release bytes."""
    gpu = manifest["gpu"]
    runtime_sources = literal(contents, "judge/container_runtime_service.py", "SOURCES")
    if not set(runtime_sources) <= set(gpu["source_scopes"]["controller"]):
        raise GPUReadinessError("Frozen runtime is outside the GPU controller scope")
    task = source_digest(contents, gpu["source_scopes"]["task"])
    combined = hashlib.sha256(
        b"praximesh-single-gpu-task-v1\0"
        + task.encode()
        + b"\0"
        + manifest_digest(gpu["driver"]).encode()
    ).hexdigest()
    if combined != gpu["contracts"]["task"]:
        raise GPUReadinessError("Frozen GPU task contract is inconsistent")
    result = {
        "task_image": manifest["images"]["gpu_task"],
        "task_contract": task,
        "runtime_contract": source_digest(contents, runtime_sources),
        "controller_contract": gpu["contracts"]["controller"],
        "driver_manifest": gpu["driver"],
        "management_manifest": gpu["management"],
        "controller_limits": literal(contents, "judge/container_controller.py", "LIMITS"),
    }
    for field, constant in (
        ("runsc_sha256", "RUNSC_SHA256"),
        ("runsc_release", "RELEASE"),
        ("nvrtc_archive_sha256", "NVRTC_ARCHIVE_SHA256"),
        ("nvrtc_libraries", "LIBRARIES"),
    ):
        result[field] = literal(contents, "judge/gpu_artifacts.py", constant)
    for field, constant in (
        ("docker_cli_version", "CLI_VERSION"),
        ("docker_cli_sha256", "CLI_SHA256"),
    ):
        result[field] = literal(contents, "judge/container_controller.py", constant)
    return result


def release_artifacts(generation, directory):
    directory = Path(directory)
    manifest = verify(directory, generation["release_sha256"])
    if manifest != generation["manifest"] or "gpu" not in manifest:
        raise GPUReadinessError("Readiness archive differs from its recorded generation")
    # verify_source checks every byte again, including changes since verify().
    contents = verify_source(
        directory / "source.tar.gz",
        manifest["source_files"],
        capture=manifest["gpu"]["source_scopes"]["controller"],
    )
    return archived_artifacts(manifest, contents)


def aware(value):
    return isinstance(value, datetime) and value.utcoffset() is not None


class GPUReadiness:
    def __init__(self, host, generation, directory):
        self.runtime = WorkerRuntime(host, generation)
        self.host = host
        self.artifacts = release_artifacts(generation, directory)

    def instance(self):
        runtime = self.runtime
        config, metadata, token = runtime.common()
        record = runtime.load()
        if (
            not config["enabled"]
            or record is None
            or record["phase"] != "running"
            or record["metadata"] != metadata
        ):
            raise GPUReadinessError("Readiness requires the original running worker generation")
        info = runtime.inspect()
        if not running(info) or info["State"]["StartedAt"] != record["started_at"]:
            raise GPUReadinessError("Original GPU worker startup instance is not running")
        return config, metadata, token, info["Id"], record["started_at"]

    def environment(self, value, fingerprint):
        runtime = self.runtime
        validate_environment(value, fingerprint)
        fd = os.open(runtime.node.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            raw = read_private(fd, "mounts.json")
        finally:
            os.close(fd)
        if hashlib.sha256(raw).hexdigest() != runtime.binding["node"]["mounts_sha256"]:
            raise GPUReadinessError("Original GPU observation metadata changed")
        observer = json.loads(raw)["device_observer"]
        engine = runtime.node.engine.verify()
        if engine["ID"] != runtime.binding["node"]["installation"]["engine"]:
            raise GPUReadinessError("Original GPU daemon changed during readiness observation")
        if (
            value["execution_digest"]
            != runtime.generation["manifest"]["gpu"]["contracts"]["execution"]
            or value["target"] != observer["target"]
            or value["device_spec"]["minor"] != observer["minor"]
            or value["artifacts"] != self.artifacts | {"engine": engine_identity(engine)}
            or value["artifacts"]["management_manifest"] != observer["library"]
        ):
            raise GPUReadinessError("Published GPU environment differs from its frozen generation")

    def valid_snapshot(self, value, instance, elapsed, *, verify_environment=True):
        config, _, _, _, started = instance
        device, now = value["device"], value["observed_at"]
        if (
            not aware(now)
            or type(value["pending"]) is not bool
            or type(value["reservations"]) is not bool
        ):
            raise GPUReadinessError("GPU database observation has no timezone-aware clock")
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if (
            device is None
            or value["pending"]
            or value["reservations"]
            or device["state"] != "ready"
            or device["id"] != config["gpu"]
            or device["worker_id"] != config["worker_id"]
            or device["job_id"] is not None
            or device["reservation_token"] is not None
            or not aware(device["checked_at"])
            or not start <= device["checked_at"] <= now
            or not 0 <= (now - device["checked_at"]).total_seconds() + elapsed < FRESH_SECONDS
        ):
            return False
        try:
            owner = str(device["owner_token"])
            if str(uuid.UUID(owner)) != owner:
                raise ValueError("Invalid owner")
            if verify_environment:
                self.environment(device["environment"], device["fingerprint"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise GPUReadinessError("Published GPU readiness evidence is invalid") from None
        return True

    def observe(self):
        """Always re-read: a prior success is never an exposure credential."""
        initial = self.instance()
        mark = time.monotonic()
        value = self.host.database.gpu_worker_snapshot(
            self.runtime.generation["database"], initial[2], initial[0]["gpu"]
        )
        if self.instance() != initial:
            raise GPUReadinessError("GPU worker generation changed during readiness observation")
        if not self.valid_snapshot(value, initial, time.monotonic() - mark):
            return None
        if self.instance() != initial:
            raise GPUReadinessError("GPU worker changed during environment validation")
        if not self.valid_snapshot(
            value, initial, time.monotonic() - mark, verify_environment=False
        ):
            return None
        # Environment/daemon checks may take time. Re-observe all reservations and
        # pause in one fresh statement, then fence the same container once more.
        mark = time.monotonic()
        confirmed = self.host.database.gpu_worker_snapshot(
            self.runtime.generation["database"], initial[2], initial[0]["gpu"]
        )
        if self.instance() != initial:
            raise GPUReadinessError("GPU worker changed during final readiness observation")
        if not self.valid_snapshot(
            confirmed, initial, time.monotonic() - mark, verify_environment=False
        ):
            return None
        device = confirmed["device"]
        if any(
            device[key] != value["device"][key]
            for key in ("owner_token", "fingerprint", "environment")
        ):
            raise GPUReadinessError("GPU owner or environment changed during readiness observation")
        return {
            "container": initial[3],
            "started_at": initial[4],
            "configuration_sha256": digest(initial[1]),
            "owner_token": str(device["owner_token"]),
            "fingerprint": device["fingerprint"],
            "checked_at": device["checked_at"].isoformat(),
        }

    def wait(self):
        initial = self.instance()
        started = datetime.fromisoformat(initial[4].replace("Z", "+00:00"))
        age = time.time() - started.timestamp()
        if age < 0:
            raise GPUReadinessError("GPU worker startup time is in the future")
        deadline = time.monotonic() + max(0, STARTUP_SECONDS - age)
        while True:
            if self.instance() != initial:
                raise GPUReadinessError("Original GPU worker changed while awaiting startup")
            result = self.observe()
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GPUReadinessError(
                    "GPU startup observation timed out; original worker retained"
                )
            time.sleep(min(0.5, remaining))
