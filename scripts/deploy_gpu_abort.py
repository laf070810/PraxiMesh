"""Retire an exited, unexposed GPU startup candidate when restoring original CPU.

No readiness, task recovery, reservation release or alternate database choice is
provided. The existing startup/recovery history is bound without rewriting it.
"""

import uuid

from judge.container_deployment_config import digest
from scripts.deploy_gpu_config import context, frozen, require_idle_journal, require_stopped_workers
from scripts.deploy_gpu_worker import HEX, ZERO, WorkerRuntime, stopped

FIELDS = {
    "schema",
    "operation",
    "generation",
    "previous_sha256",
    "previous_id",
    "binding_sha256",
    "runtime",
    "preparation_sha256",
    "recovery_sha256",
    "phase",
}
RUNTIME = {"id", "runtime_sha256", "metadata_sha256", "started_at"}
PHASES = {"prepared", "stopped", "freezing", "aborted"}


class GPUStartupAbortError(RuntimeError):
    pass


def key_for(operation, generation):
    return operation + ":" + generation["gpu_worker"]


def records(host):
    values = host.state.get("gpu_startup_aborts", {})
    if type(values) is not dict:
        raise GPUStartupAbortError("Invalid GPU startup abort journal")
    for key, value in values.items():
        validate(value)
        if key != key_for(value["operation"], value["generation"]):
            raise GPUStartupAbortError("GPU startup abort key changed")
    return values


def validate(value):
    try:
        valid = (
            type(value) is dict
            and set(value) == FIELDS
            and type(value["schema"]) is int
            and value["schema"] == 1
            and str(uuid.UUID(value["operation"])) == value["operation"]
            and set(value["generation"]) == set(context(value["generation"])) | {"web", "worker"}
            and value["phase"] in PHASES
            and type(value["previous_id"]) is str
            and len(value["previous_id"]) == 12
            and all(
                HEX.fullmatch(value[field])
                for field in ("previous_sha256", "binding_sha256", "preparation_sha256")
            )
            and (value["recovery_sha256"] is None or HEX.fullmatch(value["recovery_sha256"]))
            and type(value["runtime"]) is dict
            and set(value["runtime"]) == RUNTIME
            and all(
                HEX.fullmatch(value["runtime"][field])
                for field in ("id", "runtime_sha256", "metadata_sha256")
            )
            and value["runtime"]["started_at"] not in ZERO
        )
        from scripts.deploy_gpu_worker import started

        started(value["runtime"]["started_at"])
    except (TypeError, ValueError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise GPUStartupAbortError("GPU startup abort identity is invalid")


def history_matches(host, key, value):
    from scripts.deploy_gpu_release import validate_record as validate_release

    validate(value)
    preparation = host.state.get("gpu_preparations", {}).get(key)
    recovery = host.state.get("gpu_recoveries", {}).get(key)
    release = host.state.get("gpu_releases", {}).get(value["operation"]) or {}
    validate_release(release)
    previous = release.get("previous")
    # The coordinated journal ends both forward deployment and rollback at
    # "active"; only the public operation records "rolled_back". Bind the
    # completed recovery's original CPU target instead of trusting that phase.
    completed = (
        release["phase"] == "active"
        and bool(release["recoveries"])
        and release["recoveries"][-1]["phase"] == "finished"
        and release["recoveries"][-1]["target"] == value["previous_id"]
    )
    if (
        release.get("candidate") != value["generation"]
        or release.get("operation") != value["operation"]
        or not (release["phase"] == "recovering" or completed)
        or previous is None
        or "gpu" in previous.get("manifest", {})
        or previous.get("id") != value["previous_id"]
        or digest(previous) != value["previous_sha256"]
        or digest(preparation) != value["preparation_sha256"]
        or (None if recovery is None else digest(recovery)) != value["recovery_sha256"]
    ):
        raise GPUStartupAbortError("Original GPU startup/recovery history changed")
    return recovery


def completed_recovery_abort(host, node, binding, key, recovery):
    """Ordinary publication may accept only an actually frozen completed abort."""
    value = records(host).get(key)
    if value is None:
        return False
    if value["phase"] != "aborted" or value["binding_sha256"] != digest(binding):
        raise GPUStartupAbortError("Finish original GPU startup abort before publication")
    if history_matches(host, key, value) != recovery:
        raise GPUStartupAbortError("Aborted recovery is not its original history")
    node.require_owner(binding)
    runtime = host.state.get("gpu_runtime", {}).get(value["generation"]["gpu_worker"])
    if runtime is None or runtime["phase"] != "stopped" or snapshot(runtime) != value["runtime"]:
        raise GPUStartupAbortError("Aborted GPU startup instance changed")
    host.database.require_frozen_configuration(value["generation"]["database"])
    return True


def snapshot(runtime):
    return {
        "id": runtime["id"],
        "runtime_sha256": runtime["runtime_sha256"],
        "metadata_sha256": digest(runtime["metadata"]),
        "started_at": runtime["started_at"],
    }


def original_cpu_target(host, generation, target):
    operation = host.state.get("operation") or {}
    return (
        target is not None
        and "gpu" not in target["manifest"]
        and operation.get("previous") == target
        and host.state.get("current") == target
        and operation.get("candidate") == generation
        and operation.get("exposure_target") is None
        and operation.get("phase") == "recovering"
    )


class StartupAbort:
    """Internal context, valid only while its exact node lock is held for freezing."""

    def __init__(self, host, generation, target):
        self.host, self.generation, self.target = host, frozen(generation), frozen(target)
        self.runtime = WorkerRuntime(host, generation)
        self.key = key_for(host.state["operation"]["id"], generation)
        self.record = records(host).get(self.key)
        self.node = None

    def original(self):
        host = self.host
        host.require_locked()
        host.database._require_operation_lock()
        if not original_cpu_target(host, self.generation, self.target):
            raise GPUStartupAbortError(
                "GPU startup abort requires the original unexposed CPU rollback"
            )
        _, metadata, token = self.runtime.common()
        runtime = self.runtime.load()
        if (
            runtime is None
            or runtime["metadata"] != metadata
            or runtime["phase"] not in {"running", "start_failed", "stop_requested", "stopped"}
            or runtime["started_at"] in ZERO | {None}
        ):
            raise GPUStartupAbortError("GPU startup abort requires its original exited startup")
        info = self.runtime.inspect()
        if (
            not stopped(info)
            or info["State"]["Status"] != "exited"
            or info["State"]["StartedAt"] != runtime["started_at"]
            or info["State"].get("ExitCode") != 1
            or info["State"].get("OOMKilled") is not False
            or info["State"].get("Error") != ""
        ):
            raise GPUStartupAbortError("The original failed GPU startup is not fully exited")
        from scripts.deploy_gpu_candidate import validate_record as validate_preparation
        from scripts.deploy_gpu_recovery import validate_record as validate_recovery

        preparation = host.state.get("gpu_preparations", {}).get(self.key)
        validate_preparation(preparation)
        if (
            preparation["generation"] != self.generation
            or preparation["operation"] != token
            or any(entry["phase"] != "waiting" for entry in preparation["attempts"])
            or any(
                entry["round_id"] is not None or entry["observation"] is not None
                for entry in preparation["attempts"]
            )
            or any(
                v["generation"] == context(self.generation)
                for v in host.state.get("gpu_candidate_checks", {}).values()
            )
            or any(
                v.get("generation") == self.generation
                for v in host.state.get("gpu_exposures", {}).values()
            )
        ):
            raise GPUStartupAbortError("GPU candidate already entered verification or exposure")
        recovery = host.state.get("gpu_recoveries", {}).get(self.key)
        if recovery is not None:
            validate_recovery(recovery)
            if (
                recovery["phase"] != "waiting"
                or recovery["operation"] != token
                or recovery["container"] != runtime["id"]
                or recovery["generation"] != context(self.generation)
                or recovery["runtime_sha256"] != runtime["runtime_sha256"]
                or recovery["metadata_sha256"] != digest(metadata)
                or recovery["attempts"][-1]["started"] != runtime["started_at"]
            ):
                raise GPUStartupAbortError("GPU startup recovery changed or already completed")
        elif preparation["attempts"][-1]["startup"] != {
            "container": runtime["id"],
            "started_at": runtime["started_at"],
        }:
            raise GPUStartupAbortError("Original failed startup differs from its preparation")
        value = {
            "schema": 1,
            "operation": token,
            "generation": self.generation,
            "previous_sha256": digest(self.target),
            "previous_id": self.target["id"],
            "binding_sha256": digest(self.runtime.binding),
            "runtime": snapshot(runtime),
            "preparation_sha256": digest(preparation),
            "recovery_sha256": None if recovery is None else digest(recovery),
        }
        history_matches(host, self.key, value | {"phase": "prepared"})
        if (
            self.record is not None
            and {k: v for k, v in self.record.items() if k != "phase"} != value
        ):
            raise GPUStartupAbortError("Original GPU startup abort intent changed")
        return value

    def save(self, phase):
        self.record["phase"] = phase
        self.host.state.setdefault("gpu_startup_aborts", {})[self.key] = self.record
        self.host.save()

    def permits(self, host, node, binding, key, recovery):
        if (
            host is not self.host
            or node is not self.node
            or binding != self.runtime.binding
            or key != self.key
            or self.record["phase"] != "freezing"
        ):
            return False
        node.require_owner(binding)
        self.original()
        if (
            self.runtime.record["phase"] != "stopped"
            or history_matches(host, key, self.record) != recovery
        ):
            raise GPUStartupAbortError("GPU abort cannot bypass another recovery or a live worker")
        require_idle_journal(node)
        return True

    def run(self):
        value = self.original()
        if self.record is None:
            with self.runtime.node.locked() as node:
                node.require_owner(self.runtime.binding)
                if self.original() != value:
                    raise GPUStartupAbortError("GPU startup changed before its abort intent")
                require_idle_journal(node)
                with self.host.database.gpu_configuration_guard(
                    [self.generation["database"]], value["operation"]
                ) as check:
                    check()
            self.record = value | {"phase": "prepared"}
            self.save("prepared")
        if self.record["phase"] == "aborted":
            with self.runtime.node.locked() as node:
                node.require_owner(self.runtime.binding)
                self.original()
                require_idle_journal(node)
                require_stopped_workers(self.host, node, self.runtime.binding)
                self.host.database._require_frozen(self.generation["database"])
            return
        if self.runtime.record["phase"] != "stopped":
            self.runtime.stop()  # Only reconciles the already-exited original instance.
        if self.record["phase"] == "prepared":
            self.save("stopped")
        with self.runtime.node.locked() as node:
            self.node = node
            try:
                node.require_owner(self.runtime.binding)
                self.original()
                require_idle_journal(node)
                with self.host.database.gpu_configuration_guard(
                    [self.generation["database"]], self.record["operation"]
                ) as check:
                    check()
                self.host.stop(self.generation)  # No queue row lock spans this wait.
                if self.record["phase"] == "stopped":
                    self.save("freezing")
                require_stopped_workers(self.host, node, self.runtime.binding, abort=self)
                # This rechecks pause/drain under the original locks, and holds its
                # transaction through CONNECT revocation and session termination.
                self.host.database.freeze_gpu(self.generation["database"], self.record["operation"])
                self.original()
                require_idle_journal(node)
                require_stopped_workers(self.host, node, self.runtime.binding, abort=self)
                self.host.database._require_frozen(self.generation["database"])
                self.save("aborted")
            finally:
                self.node = None


def maybe_abort(host, generation, target):
    if not original_cpu_target(host, generation, target):
        return False
    key = key_for(host.state["operation"]["id"], generation)
    preparation = host.state.get("gpu_preparations", {}).get(key)
    if key not in records(host) and (
        preparation is None or preparation["attempts"][-1]["phase"] != "waiting"
    ):
        return False
    abort = StartupAbort(host, generation, target)
    abort.runtime.load()
    info = abort.runtime.inspect()
    if key not in records(host):
        if (
            not stopped(info)
            or info["State"].get("ExitCode") != 1
            or info["State"].get("OOMKilled") is not False
            or info["State"].get("Error") != ""
        ):
            return False
    abort.run()
    return True


def require_not_aborted(host, generation):
    if any(context(value["generation"]) == context(generation) for value in records(host).values()):
        raise GPUStartupAbortError(
            "GPU startup abort permits only original CPU rollback, not startup"
        )
