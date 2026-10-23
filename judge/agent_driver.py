"""Trusted short-budget WASI driver; never runs learner code in host Python."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge import wasi_runner  # noqa: E402
from judge.agent_policy import PROFILE  # noqa: E402

if __name__ == "__main__":
    # These assignments affect this dedicated process only. The shared driver
    # enforces them in actual Wasmtime fuel, timers, rlimits and output callbacks.
    wasi_runner.FUEL = PROFILE["turn_fuel"]
    wasi_runner.WALL_SECONDS = PROFILE["turn_wall_seconds"]
    wasi_runner.HOST_CPU_SECONDS = PROFILE["turn_cpu_soft_seconds"]
    wasi_runner.OUTPUT = PROFILE["turn_output_bytes"]
    wasi_runner.INPUT = PROFILE["input_bytes"]
    try:
        code = wasi_runner.main()
    except BaseException:
        code = 70
    sys.stdout.buffer.flush()
    os._exit(code)
