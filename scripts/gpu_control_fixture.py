"""Fixed CPU-only process tree for task-service acceptance; no learner code."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mark(path):
    values = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "start_ticks": values[19]}))
    temporary.replace(path)


def main():
    mode, destination = sys.argv[1:]
    destination = Path(destination)
    if mode == "owner":
        from judge.gpu_device_lock import DeviceLock
        from judge.gpu_service import OwnedJobService

        config = json.loads((destination / "config.json").read_text())
        with DeviceLock(destination / "locks", config["device"]) as guard:
            service = OwnedJobService(guard, config["job"], config["reservation"])
            state = service.start(
                [sys.executable, str(Path(__file__).resolve()), "tree", str(destination)],
                deadline=time.monotonic() + 60,
                on_tick=lambda: None,
            )
            print(
                json.dumps({"unit": service.unit, "invocation": state["InvocationID"]}), flush=True
            )
            # Deliberately lose the controller without stopping its own service.
        return
    if mode not in {"tree", "leaf"}:
        raise ValueError("Unknown fixed CPU fixture")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "leaf":
        os.setsid()  # A separate process group still belongs to the task cgroup.
    else:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "leaf", str(destination)])
    mark(destination / (mode + ".json"))
    while not (destination / "exit-parent").exists() or mode == "leaf":
        time.sleep(0.05)


if __name__ == "__main__":
    main()
