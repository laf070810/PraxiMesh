"""Fixed private controller endpoint; a host launcher is still required."""

import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_controller import ContainerController, failure_locations  # noqa: E402
from judge.container_practice_protocol import invocation, serve  # noqa: E402
from judge.container_practice_state import LocalPracticeState, require  # noqa: E402
from judge.container_practice_transport import PipeChannel  # noqa: E402
from judge.container_processes import ProcessDisappeared, process_failure_details  # noqa: E402
from judge.gpu_device_lock import DeviceLockError  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402


@contextmanager
def cancellation():
    cancelled = False

    def stop(_number, _frame):
        nonlocal cancelled
        cancelled = True

    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)

        def check():
            if cancelled:
                raise InterruptedError("Local GPU operation cancelled")

        yield check
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 4:
            raise ValueError("Expected mode, device, nonce and absolute deadline")
        mode, device, nonce, raw_deadline = args
        deadline = float(raw_deadline)
        invocation(mode, device, nonce, deadline)
        with cancellation() as cancelled, ContainerController(device) as control:
            require(control.saved["version"] == 4, "Local GPU operation requires original v4 state")
            with LocalPracticeState(control.journal, create=mode == "initialize") as state:

                def check():
                    cancelled()
                    require(time.monotonic() < deadline, "Local invocation deadline expired")
                    control.check()
                    state.check()

                with PipeChannel(0, 1, deadline=deadline, on_tick=check) as channel:
                    try:
                        serve(control, state, channel, mode=mode, nonce=nonce, on_tick=check)
                    except (OSError, ValueError, DeviceLockError, SandboxUnavailable) as error:
                        kind = (
                            "ProcessDisappeared"
                            if type(error) is ProcessDisappeared
                            else "SandboxUnavailable"
                            if isinstance(error, SandboxUnavailable)
                            else "DeviceLockError"
                            if isinstance(error, DeviceLockError)
                            else "OSError"
                            if isinstance(error, OSError)
                            else "ValueError"
                        )
                        try:
                            control.journal.write(
                                "local-failure." + nonce + ".json",
                                {
                                    "schema": 1,
                                    "scope": "praximesh-local-failure-v1",
                                    "mode": mode,
                                    "nonce": nonce,
                                    "error": kind,
                                    "origin": failure_locations(error),
                                    "process": process_failure_details(error),
                                },
                                create=True,
                            )
                        except Exception:
                            pass  # Preserve the original failure if its state cannot be written.
                        raise
        return 0
    except InterruptedError:
        print("Local operation cancelled; retain original state for recovery.", file=sys.stderr)
        return 130
    except (OSError, ValueError, DeviceLockError, SandboxUnavailable):
        # No source, arbitrary frame, configuration or raw driver error in logs.
        print("Local controller unavailable; retain original state for recovery.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
