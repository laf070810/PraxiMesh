"""CPU-only replay of the same resource checks used by the GPU controller."""

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_environment import PrivateRuntime  # noqa: E402
from judge.gpu_isolation import check_resources  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402


def main():
    base, socket_path, runtime_archive, image_archive, deadline = sys.argv[1:]
    base = Path(base)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=float(deadline), on_tick=lambda: None)
    runtime = None
    phase = "prepare"
    resources = {}
    record = {"passed": False}
    try:
        channel.send({"event": "connected"})
        runtime = PrivateRuntime(base, deadline=float(deadline), on_tick=channel.connected)
        record["identity"] = runtime.prepare(Path(runtime_archive), Path(image_archive))
        channel.send({"event": "ready", "identity": record["identity"]})
        request = channel.receive()
        phase = "resource-checks"
        check_resources(runtime, request, expect_gpu=False, record=resources)
        phase = "store-cleanup"
        runtime.close()
        record.update(passed=True, private_store_removed=True)
    except BaseException as error:
        record.update(phase=phase, error=type(error).__name__ + ": " + str(error))
        if getattr(error, "diagnostics", None):
            # Only reviewed fixed CPU programs run in this acceptance entry.
            record["diagnostics"] = error.diagnostics.decode(errors="replace")[-4096:]
        if runtime is not None:
            try:
                runtime.close()
                record["private_store_removed"] = True
            except BaseException as cleanup_error:
                record["cleanup_error"] = type(cleanup_error).__name__ + ": " + str(cleanup_error)
    finally:
        record.update(resources)
        channel.send({"event": "finished", "result": record})
        if channel.receive() != {"event": "finish"}:
            raise ValueError("Unexpected completion acknowledgement")
        channel.close()
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
