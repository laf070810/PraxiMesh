"""Fixed CPU maintenance peer for real Docker pipes; never executes learner code."""

import json
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_controller import (  # noqa: E402
    SOCKET,
    STATE,
    ContainerController,
    environment,
    validate_mounts,
)
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_practice_protocol import invocation, serve  # noqa: E402
from judge.container_practice_state import LocalPracticeState  # noqa: E402
from judge.container_practice_transport import PipeChannel  # noqa: E402
from judge.container_processes import HostProcessView  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402


def main():
    mode, device, nonce, raw = sys.argv[1:]
    deadline = float(raw)
    invocation(mode, device, nonce, deadline)
    assert mode in {"initialize", "recover"}  # No run path in this CPU fixture.
    saved = validate_mounts(json.loads((STATE / "mounts.json").read_bytes()), device)
    environment(saved["operator"])
    case = json.loads((STATE / "journal/fixture-case.json").read_bytes())["case"]
    assert case in {"normal", "exit-error", "extra-output", "hold"}
    with closing(HostProcessView(saved["host_processes"])) as view:
        with DeviceLock(STATE / "locks", device) as guard:
            with ContainerJournal(STATE / "journal", guard, LocalEngine(SOCKET)) as journal:
                with LocalPracticeState(journal) as state:
                    # Explicit CPU test double: real mounts/locks/journal/proc and
                    # real serve(), without constructing the device observer.
                    control = ContainerController.__new__(ContainerController)
                    control.guard, control.journal = guard, journal
                    control.check = lambda: (view.check(), state.check())
                    journal.write("fixture-started.json", {"nonce": nonce})
                    if case == "hold":
                        while time.monotonic() < deadline:
                            control.check()
                            time.sleep(0.1)
                    else:
                        with PipeChannel(0, 1, deadline=deadline, on_tick=control.check) as channel:
                            serve(
                                control,
                                state,
                                channel,
                                mode=mode,
                                nonce=nonce,
                                on_tick=control.check,
                            )
                        if case == "extra-output":
                            sys.stdout.buffer.write(b"unexpected")
                            sys.stdout.buffer.flush()
                        if case == "exit-error":
                            return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
