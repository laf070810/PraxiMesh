"""Controller-owned CUDA session profile; no GPU submission admission yet."""

import hashlib
import json
from pathlib import Path

from judge.gpu_contract import BATCH_REPETITIONS, MESSAGE_BYTES

PROFILE = {
    "id": "cuda-session-v1",
    "compile_seconds": 15,
    "request_seconds": 5,
    "run_seconds": 60,
    "submit_seconds": 120,
    "heartbeat_seconds": 0.25,
    "frame_bytes": MESSAGE_BYTES,
    "stdout_bytes": 2 * 1024 * 1024,
    "stderr_bytes": 32 * 1024,
    "warmup_batches": 2,
    "sample_batches": 5,
    "batch_repetitions": BATCH_REPETITIONS,
    "measurement": "controller-roundtrip-batch-wall-v1",
}


def profile_digest():
    return hashlib.sha256(
        json.dumps(PROFILE, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def execution_digest(root=None):
    """Bind the implemented session and probe isolation, not a formal receipt."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    fingerprint = hashlib.sha256()
    for relative in (
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
        "judge/gpu_controller_entry.py",
        "judge/gpu_adapter.py",
        "judge/gpu_isolation.py",
        "judge/gpu_isolation_guest.py",
        "judge/gpu_host_pids.py",
        "judge/gpu_kernel_checks.py",
        "judge/gpu_lifecycle.py",
        "judge/gpu_receipt.py",
        "judge/gpu_probes.py",
        "judge/gpu_device_lock.py",
        "judge/results.py",
        "core/gpu_resources.py",
        "core/gpu_control.py",
        "core/gpu_execution.py",
        "core/gpu_startup.py",
        "core/gpu_readiness.py",
        "core/resources.py",
        "core/queue.py",
        "core/models.py",
        "scripts/gpu_environment_probe.py",
        "scripts/gpu_kernel_probe.py",
        "scripts/gpu_service_entry.py",
        "requirements.lock",
        "requirements-judge.lock",
        "uv.lock",
    ):
        fingerprint.update(relative.encode() + b"\0")
        fingerprint.update((root / relative).read_bytes())
    return fingerprint.hexdigest()
