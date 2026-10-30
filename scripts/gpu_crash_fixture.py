"""Reviewed CPU guest interrupted while live; deliberately leaves mapped storage."""

import os
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_environment import PrivateRuntime  # noqa: E402
from judge.gpu_session import GPUSession  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402


def main():
    base, socket_path, runtime_archive, image_archive, deadline = sys.argv[1:]
    base = Path(base)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=float(deadline), on_tick=lambda: None)
    try:
        channel.send({"event": "connected"})
        runtime = PrivateRuntime(base, deadline=float(deadline), on_tick=channel.connected)
        identity = runtime.prepare(Path(runtime_archive), Path(image_archive))
        fixture = base / "cpu-guest.py"
        shutil.copyfile(ROOT / "scripts/gpu_runtime_cpu_guest.py", fixture)
        fixture.chmod(0o444)
        # This fixed marker gives the host a direct, reproducible permission
        # failure in addition to the real container's abandoned image layers.
        mapped = base / "data/mapped-proof"
        mapped.mkdir(mode=0o700)
        (mapped / "owned").write_text("fixed mapped-file recovery check")
        os.chown(mapped, 65534, 65534)
        runtime.create(
            "live",
            "/usr/local/bin/python",
            ["-I", "/opt/fixture.py"],
            mounts=[(fixture, "/opt/fixture.py")],
        )
        active = GPUSession(
            runtime.attach("live"), lambda: None, architecture=[], mode="run", on_tick=runtime.tick
        )
        message, _elapsed = active.exchange({"operation": "started-hang"}, seconds=5)
        assert message == {"event": "busy"}
        assert runtime.inspect("live")["State"]["Running"]
        channel.send(
            {
                "event": "live",
                "identity": identity,
                "container_id": runtime.containers["live"]["id"],
            }
        )
        request = channel.receive()
        if request == {"event": "exit"}:
            os._exit(37)  # Skip Python cleanup with a real guest still running.
        if request != {"event": "wait"}:
            raise ValueError("Unknown fixed crash scenario")
        time.sleep(120)  # Independent service lifetime must stop the process tree.
        raise RuntimeError("The owned service did not enforce its hard lifetime")
    except Exception as error:
        channel.send({"event": "failed", "error": type(error).__name__})
        raise


if __name__ == "__main__":
    main()
