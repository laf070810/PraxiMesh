"""Trusted compilation transport; all source reaches NVRTC only inside runsc."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_compile_protocol import (  # noqa: E402
    validate_compiled,
    validate_identity,
    validate_request,
)
from judge.container_contract import PYTHON  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.container_transport import ContainerListener  # noqa: E402
from judge.gpu_contract import compile_request  # noqa: E402
from judge.gpu_policy import PROFILE  # noqa: E402
from judge.gpu_session import GPUExecutionError  # noqa: E402


def main():
    # The fixed deadline fault is selected by trusted acceptance code, never a message.
    mode = sys.argv[1] if len(sys.argv) == 2 else "normal" if len(sys.argv) == 1 else None
    if mode not in {"normal", "deadline"}:
        raise ValueError("Unexpected fixed compiler mode")
    listener = ContainerListener(on_tick=lambda: None)
    channel = None
    try:
        channel = listener.accept()
        try:
            request = validate_request(channel.receive())
        except ValueError:
            channel.send(
                {
                    "event": "compile-rejected",
                    "status": "invalid_request",
                    "guest_started": False,
                    "ready": False,
                }
            )
            if channel.receive() != {"event": "acknowledged"}:
                raise ValueError("Unexpected rejection acknowledgement") from None
            return
        runtime = ContainerRuntime(deadline=listener.status["deadline"] - 3, on_tick=lambda: None)
        runtime.create(
            "compiler", PYTHON, ["-I", "/opt/praximesh/judge/container_compile_guest.py"]
        )
        cleanup = []

        def tick():
            runtime.tick()
            channel.connected()

        report = {"event": "compile-checked", "ready": False, "executed": False}
        try:
            with runtime.session(
                "compiler", on_cleanup=cleanup.append, mode="submit", on_tick=tick
            ) as session:
                identity, _ = session.exchange(
                    {"protocol": 1, "operation": "prepare-compiler"},
                    seconds=PROFILE["compile_seconds"],
                )
                validate_identity(identity)
                channel.send(identity)
                if channel.receive() != {"event": "begin"}:
                    raise ValueError("Unexpected compiler acknowledgement")
                value, _ = session.exchange(
                    {
                        **compile_request(request["source"], request["operation"]),
                        "architecture": request["architecture"],
                    },
                    seconds=PROFILE["compile_seconds"],
                )
                validate_compiled(value, request["architecture"])
                report.update(status="compiled", compilation=value)
                del request
                channel.send({"event": "compiled", "compilation": value})
                if mode == "deadline":
                    while True:
                        time.sleep(0.05)
                if channel.receive() != {"event": "finish"}:
                    raise ValueError("Unexpected compilation completion")
                session.finish()
        except GPUExecutionError as error:
            report.update(status=error.verdict)
        except Exception as error:
            report.update(status="platform_error", failure_kind=type(error).__name__)
        if cleanup != [{"id": "compiler", "stopped": True, "removed": True}]:
            raise RuntimeError("Compiler guest cleanup was not confirmed")
        report["cleanup"] = cleanup
        channel.send(report)
        if channel.receive() != {"event": "acknowledged"}:
            raise ValueError("Unexpected result acknowledgement")
    finally:
        if channel is not None:
            channel.close()
        listener.close()


if __name__ == "__main__":
    main()
