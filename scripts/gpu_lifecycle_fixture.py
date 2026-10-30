"""Fixed CPU-only guest for the shared controller-fault protocol."""

import os
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_environment import PrivateRuntime  # noqa: E402
from judge.gpu_lifecycle import require, trigger_fault  # noqa: E402
from judge.gpu_session import GPUSession  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402


def main():
    base, socket_path, runtime_archive, image_archive, deadline, mode = sys.argv[1:]
    if mode not in {"fault", "cpu-recovery"}:
        raise ValueError("Only fixed CPU lifecycle modes are supported")
    base = Path(base)
    deadline = float(deadline)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=deadline, on_tick=lambda: None)
    runtime = None
    closed = False
    cleanup = []
    try:
        channel.send({"event": "connected"})
        runtime = PrivateRuntime(base, deadline=deadline, on_tick=channel.connected)
        identity = runtime.prepare(Path(runtime_archive), Path(image_archive))
        require(runtime.target is None, "CPU lifecycle fixture must not prepare a GPU")
        fixture = base / "cpu-guest.py"
        shutil.copyfile(ROOT / "scripts/gpu_runtime_cpu_guest.py", fixture)
        fixture.chmod(0o444)
        if mode == "fault":
            mapped = base / "data/mapped-proof"
            mapped.mkdir(mode=0o700)
            (mapped / "owned").write_text("fixed lifecycle storage recovery check")
            os.chown(mapped, 65534, 65534)
        container_id = runtime.create(
            "live",
            "/usr/local/bin/python",
            ["-I", "/opt/fixture.py"],
            mounts=[(fixture, "/opt/fixture.py")],
        )

        def stop():
            value = runtime.stop_container("live")
            cleanup.append(value)
            return value

        with GPUSession(
            runtime.attach("live"),
            stop,
            architecture=[],
            mode="run",
            on_tick=runtime.tick,
        ) as session:
            operation = "started-hang" if mode == "fault" else "busy"
            message, _elapsed = session.exchange({"operation": operation}, seconds=5)
            require(
                message == {"event": "busy" if mode == "fault" else "busy-finished"},
                "Fixed CPU lifecycle guest did not respond",
            )
            require(runtime.inspect("live")["State"]["Running"], "CPU guest is no longer live")
            if mode == "fault":
                channel.send(
                    {
                        "event": "live",
                        "identity": identity,
                        "container_id": container_id,
                        "gpu_prepared": False,
                    }
                )
                trigger_fault(channel, channel.receive())
                raise RuntimeError("Fixed fault unexpectedly returned")
            session.finish()
        runtime.close()
        closed = True
        channel.send(
            {
                "event": "cpu-recovered",
                "identity": identity,
                "container_id": container_id,
                "gpu_prepared": False,
                "cleanup": cleanup,
            }
        )
        require(channel.receive() == {"event": "finish"}, "Unexpected CPU recovery completion")
    finally:
        try:
            if runtime is not None and not closed:
                runtime.close()
        finally:
            channel.close()


if __name__ == "__main__":
    main()
