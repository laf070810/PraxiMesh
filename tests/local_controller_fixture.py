"""Fixed CPU fixture for outer-controller lifecycle acceptance, never a runner."""

import json
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_controller import SOCKET, STATE, environment, validate_mounts  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_practice_protocol import invocation  # noqa: E402
from judge.container_practice_state import LocalPracticeState  # noqa: E402
from judge.container_processes import HostProcessView  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402


def main():
    mode, device, nonce, raw_deadline = sys.argv[1:]
    deadline = float(raw_deadline)
    invocation(mode, device, nonce, deadline)
    saved = validate_mounts(json.loads((STATE / "mounts.json").read_bytes()), device)
    observed = environment(saved["operator"])
    # Validate procfs and the real shared lock, without a device observer.
    with closing(HostProcessView(saved["host_processes"])):
        with DeviceLock(STATE / "locks", device) as guard:
            with ContainerJournal(STATE / "journal", guard, LocalEngine(SOCKET)) as journal:
                with LocalPracticeState(journal) as state:
                    print(
                        json.dumps(
                            {
                                "event": "cpu-fixture-ready",
                                "nonce": nonce,
                                "marker": state.marker,
                                "environment": observed,
                            }
                        ),
                        flush=True,
                    )
                    # Intentionally survive an attached CLI crash; the parent
                    # must stop this exact container using its durable record.
                    while time.monotonic() < deadline:
                        time.sleep(0.1)
                        state.check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
