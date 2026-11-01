"""Original GPU shutdown and drained database freeze; no cached readiness proof.

Internal coordination only. Public GPU activation remains closed until the
complete release and recovery workflows have been validated.
"""

from scripts.deploy_gpu_config import frozen, require_idle_journal, require_stopped_workers
from scripts.deploy_gpu_worker import WorkerRuntime, stopped


class GPUQuiescenceError(RuntimeError):
    pass


def freeze(host, generation):
    generation = frozen(generation)
    runtime = WorkerRuntime(host, generation)

    def original():
        config, metadata, token = runtime.common()
        operation = host.state["operation"]
        current = host.state.get("current")
        exposed = operation.get("exposure_target")
        if exposed and (exposed != generation["id"] or not current or current != generation):
            raise GPUQuiescenceError("Cannot freeze a database preceding the exposure decision")
        prefix = f"px-{host.config['instance']}-{generation['id']}"
        if any(
            generation.get(role) != prefix + "-" + role for role in ("web", "worker")
        ) or not any(
            value == generation
            for value in (current, operation.get("previous"), operation.get("candidate"))
        ):
            raise GPUQuiescenceError("GPU freeze requires the recorded original generation")
        record = runtime.load()
        if (
            not config["enabled"]
            or record is None
            or record["metadata"] != metadata
            or record["phase"]
            not in {"created", "running", "start_failed", "stop_requested", "stopped"}
        ):
            raise GPUQuiescenceError("Reconcile original GPU creation/start before freezing")
        info = runtime.inspect()
        return (token, metadata, record["id"], record["runtime_sha256"], info["State"]["StartedAt"])

    identity = original()
    token = identity[0]
    # A failed drain leaves the running worker available to finish/recover work.
    # Neither this transaction nor the device lock spans the wait for stop.
    with host.database.gpu_configuration_guard([generation["database"]], token) as check:
        check()
    if original() != identity:
        raise GPUQuiescenceError("Original GPU instance changed before shutdown")
    runtime.stop()
    with runtime.node.locked() as node:

        def check_stopped():
            node.require_owner(runtime.binding)
            if original() != identity or not stopped(runtime.inspect()):
                raise GPUQuiescenceError("Original GPU instance changed during database freeze")
            require_stopped_workers(host, node, runtime.binding)
            require_idle_journal(node)

        check_stopped()
        host.stop(generation)
        check_stopped()
        host.database.freeze_gpu(generation["database"], token)
        check_stopped()
        # Post-freeze identity checks use short owner connections. They must all
        # have closed before a caller can proceed to backup or configuration.
        host.database._require_frozen(generation["database"])
