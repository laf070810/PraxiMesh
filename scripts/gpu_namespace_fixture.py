"""Fixed CPU-only namespace/transport fixture; accepts no executable or GPU input."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_namespace import prepare_workload_cgroup  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402


def main():
    if len(sys.argv) != 3:
        raise ValueError("Only the private socket path and fixed scenario are accepted")
    path, scenario = sys.argv[1:]
    if scenario not in {"roundtrip", "disconnect", "deadline", "extra-message"}:
        raise ValueError("Unknown fixed namespace fixture")
    limits = prepare_workload_cgroup()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(path)
    channel = Channel(sock, deadline=time.monotonic() + 30, on_tick=lambda: None)
    try:
        channel.send(
            {
                "event": "ready",
                "uid": os.getuid(),
                "cgroup": Path("/proc/self/cgroup").read_text().strip(),
                "namespaces": {
                    name: os.readlink("/proc/self/ns/" + name)
                    for name in ("user", "mnt", "pid", "net", "ipc", "uts", "cgroup")
                },
                "workload_limits": limits,
                "interfaces": sorted(
                    line.split(":", 1)[0].strip()
                    for line in Path("/proc/net/dev").read_text().splitlines()[2:]
                ),
                "environment_names": sorted(os.environ),
            }
        )
        request = channel.receive()
        if set(request) != {"canary", "values"}:
            raise ValueError("Invalid fixed transport fixture request")
        # Nothing in the request is evaluated, imported, compiled or written.
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        channel.send({"event": "received", "sha256": hashlib.sha256(encoded).hexdigest()})
        del request, encoded
        if scenario == "roundtrip":
            if channel.receive() != {"event": "finish"}:
                raise ValueError("Unexpected final fixture request")
            return
        # The leaf is intentionally detached from the parent's process group.
        # OwnedJobService must still stop it through the task cgroup.
        subprocess.Popen(["/usr/bin/sleep", "30"], start_new_session=True)
        channel.send({"event": "waiting"})
        if scenario == "deadline":
            time.sleep(30)  # Deliberately stop polling; systemd must enforce time.
        else:
            while True:
                try:
                    channel.connected()
                except SandboxUnavailable:
                    return
                time.sleep(0.02)
    finally:
        # Leave the leaf running until service exit triggers cgroup termination.
        channel.close()


if __name__ == "__main__":
    main()
