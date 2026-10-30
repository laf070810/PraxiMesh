"""Only trusted service arguments enter here; task source arrives by private IPC."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.gpu_controller import serve  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402


def main():
    base, socket_path, runtime_archive, image_archive, nvrtc_archive, deadline = sys.argv[1:]
    deadline = float(deadline)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=deadline, on_tick=lambda: None)
    try:
        serve(
            channel,
            Path(base),
            tuple(map(Path, (runtime_archive, image_archive, nvrtc_archive))),
            deadline,
        )
    except Exception:
        try:
            channel.send({"event": "failed"})
        except SandboxUnavailable:
            pass
        raise
    finally:
        channel.close()


if __name__ == "__main__":
    main()
