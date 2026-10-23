"""Versioned, server-owned resource profile for deterministic tool agents."""

import hashlib
import json

PROFILE = {
    "id": "wasi-agent-v1",
    "environment": "approved-ledger-v1",
    "max_turns": 6,
    "max_episodes": 8,
    "max_tool_cost": 7,
    "turn_fuel": 2_500_000_000,
    "episode_fuel_reserved": 15_000_000_000,
    "turn_wall_seconds": 5,
    "episode_wall_seconds": 20,
    "turn_cpu_soft_seconds": 1,
    "turn_cpu_hard_seconds": 2,
    "episode_cpu_seconds": 6,
    "cpu_sample_seconds": 0.02,
    "turn_output_bytes": 4096,
    "episode_output_reserved": 24576,
    "input_bytes": 131072,
    "state_bytes": 1024,
    "state_depth": 6,
    "state_nodes": 128,
}


def profile_digest():
    return hashlib.sha256(
        json.dumps(PROFILE, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
