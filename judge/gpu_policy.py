"""Controller-owned CUDA session profile; no GPU submission admission yet."""

import hashlib
import json
from pathlib import Path

SESSION_V1 = {
    "id": "cuda-session-v1",
    "compile_seconds": 15,
    "request_seconds": 5,
    "run_seconds": 60,
    "submit_seconds": 120,
    "heartbeat_seconds": 0.25,
    "frame_bytes": 256 * 1024,
    "stdout_bytes": 2 * 1024 * 1024,
    "stderr_bytes": 32 * 1024,
    "warmup_batches": 2,
    "sample_batches": 5,
    "batch_repetitions": 16,
    "measurement": "controller-roundtrip-batch-wall-v1",
}
# Keep completed records interpretable when a later session profile is added.
# Existing version definitions are immutable; new semantics require a new ID.
PROFILES = {SESSION_V1["id"]: SESSION_V1}
PROFILE = SESSION_V1


def profile_digest(profile=None):
    return hashlib.sha256(
        json.dumps(
            PROFILE if profile is None else profile, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


EXECUTION_SOURCES = (
    "judge/gpu.py",
    "judge/gpu_contract.py",
    "judge/gpu_runtime.py",
    "judge/gpu_policy.py",
    "judge/gpu_session.py",
    "judge/gpu_service.py",
    "judge/gpu_transport.py",
    "judge/gpu_monitor.py",
    "judge/gpu_namespace.py",
    "judge/gpu_artifacts.py",
    "judge/gpu_command.py",
    "judge/gpu_environment.py",
    "judge/gpu_recovery.py",
    "judge/gpu_recovery_entry.py",
    "judge/gpu_execution_contract.py",
    "judge/gpu_controller.py",
    "judge/gpu_task_protocol.py",
    "judge/gpu_recovery_kernel.py",
    "judge/gpu_controller_entry.py",
    "judge/gpu_adapter.py",
    "judge/gpu_isolation.py",
    "judge/gpu_isolation_guest.py",
    "judge/gpu_host_pids.py",
    "judge/gpu_kernel_checks.py",
    "judge/gpu_lifecycle.py",
    "judge/controller_faults.py",
    "judge/container_lifecycle.py",
    "judge/container_readiness.py",
    "judge/container_startup_suite.py",
    "judge/container_practice_state.py",
    "judge/container_practice.py",
    "judge/container_practice_protocol.py",
    "judge/container_practice_transport.py",
    "judge/container_practice_host.py",
    "scripts/container_practice_entry.py",
    "judge/gpu_receipt.py",
    "judge/gpu_probes.py",
    "judge/gpu_device_lock.py",
    "judge/container_contract.py",
    "judge/container_service.py",
    "judge/container_runtime_service.py",
    "judge/container_oci.py",
    "judge/container_resources.py",
    "judge/container_isolation.py",
    "judge/container_isolation_protocol.py",
    "judge/container_isolation_guest.py",
    "judge/container_journal.py",
    "judge/container_devices.py",
    "judge/container_controller.py",
    "judge/container_connection.py",
    "judge/container_transport.py",
    "judge/container_bridge.py",
    "judge/container_processes.py",
    "judge/container_monitor.py",
    "judge/container_gpu.py",
    "judge/container_gpu_runtime.py",
    "judge/container_execution.py",
    "judge/container_environment.py",
    "judge/container_adapter.py",
    "judge/container_kernel_protocol.py",
    "judge/container_kernel_flow.py",
    "judge/container_kernel_checks.py",
    "judge/container_kernel_entry.py",
    "judge/container_kernel_guest.py",
    "judge/nvml_artifacts.py",
    "judge/nvml_reader.py",
    "judge/smi_inventory.py",
    "judge/driver_artifacts.py",
    "judge/results.py",
    "core/gpu_resources.py",
    "core/gpu_control.py",
    "core/gpu_execution.py",
    "core/gpu_startup.py",
    "core/container_startup.py",
    "core/gpu_container_recovery.py",
    "core/gpu_identity.py",
    "core/gpu_readiness.py",
    "core/gpu_worker.py",
    "core/gpu_evidence.py",
    "core/gpu_admission.py",
    "core/result_evidence.py",
    "core/management/commands/gpu_worker.py",
    "core/management/commands/container_gpu_worker.py",
    "core/container_worker_database.py",
    "judge/container_worker_config.py",
    "judge/container_deployment_config.py",
    "judge/container_worker_image.py",
    "praximesh/container_worker_settings.py",
    "scripts/container_worker_entry.py",
    "core/resources.py",
    "core/queue.py",
    "core/models.py",
    "scripts/gpu_environment_probe.py",
    "scripts/gpu_kernel_probe.py",
    "scripts/gpu_service_entry.py",
    "requirements.lock",
    "requirements-judge.lock",
    "uv.lock",
)


def execution_digest(root=None):
    """Bind the implemented session and probe isolation, not a formal receipt."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    fingerprint = hashlib.sha256()
    for relative in EXECUTION_SOURCES:
        fingerprint.update(relative.encode() + b"\0")
        fingerprint.update((root / relative).read_bytes())
    return fingerprint.hexdigest()
