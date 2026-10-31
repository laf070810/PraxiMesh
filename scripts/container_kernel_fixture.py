"""Fixed CPU-only entry for the shared kernel flow; never a GPU task image."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_kernel_flow import serve_kernel  # noqa: E402
from judge.container_kernel_protocol import FINISH_MARGIN  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.container_transport import ContainerListener  # noqa: E402

IDENTITY = {"event": "cpu-kernel-ready", "scope": "protocol-fixture-only", "gpu_devices": []}


class FixedRuntime(ContainerRuntime):
    def __init__(self, mode, **kwargs):
        super().__init__(**kwargs)
        self.mode = mode
        self.kernel_cleanup = []

    def kernel_session(self, label, *, mode, on_tick=None):
        self.create(label, PYTHON, ["-I", "/opt/praximesh/kernel_cpu_guest.py", self.mode, label])
        return self.session(
            label,
            mode=mode,
            on_tick=on_tick or self.tick,
            on_cleanup=self.kernel_cleanup.append,
            architecture=[8, 6],
        )


def main():
    mode = sys.argv[1]
    if mode not in {
        "normal",
        "wrong-answer",
        "syntax-error",
        "runtime-error",
        "recovery-fail",
        "hang",
        "check-hang",
    }:
        raise ValueError("Unexpected fixed CPU kernel scenario")
    listener = ContainerListener(on_tick=lambda: None)
    channel = runtime = None
    report = {"passed": False, "gpu_executed": False, "fixture_only": True}
    try:
        channel = listener.accept()
        if channel.receive() != {"event": "prepare-cpu-fixture"}:
            raise ValueError("Unexpected CPU fixture preparation")
        runtime = FixedRuntime(
            mode, deadline=listener.status["deadline"] - FINISH_MARGIN, on_tick=channel.connected
        )
        serve_kernel(channel, runtime, IDENTITY)
        report["passed"] = True
    except BaseException as error:
        report["failure_kind"] = type(error).__name__
        report["failure"] = str(error)[:180]
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            if runtime is not None:
                report["cleanup"] = runtime.kernel_cleanup
                report["guest_cleanup_evidence"] = {
                    label: value.get("cleanup_evidence")
                    for label, value in runtime.containers.items()
                }
            if channel is not None:
                channel.close()
            listener.close()
            (Path(STATE) / "kernel-fixture.json").write_text(json.dumps(report))
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
