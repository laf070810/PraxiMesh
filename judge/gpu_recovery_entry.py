"""Fixed, credential-free file cleanup entry; no GPU, Podman, or task execution."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.gpu_recovery import remove_runtime_files  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402


def main():
    socket_path, deadline = sys.argv[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=float(deadline), on_tick=lambda: None)
    try:
        channel.send({"event": "ready"})
        request = channel.receive()
        try:
            removed = remove_runtime_files(request, channel.connected)
        except Exception:
            channel.send({"event": "failed"})
            raise
        channel.send({"event": "removed", "directories": removed})
        if channel.receive() != {"event": "finish"}:
            raise ValueError("Unexpected storage recovery acknowledgement")
    finally:
        channel.close()


if __name__ == "__main__":
    main()
