"""Fixed resource probes; execute only inside the reviewed runsc container."""

import csv
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def identity(request):
    try:
        Path("/praximesh-readonly-check").write_text("fixed infrastructure fixture")
        root_write = True
    except OSError:
        root_write = False
    try:
        connection = socket.create_connection(("127.0.0.1", request["port"]), timeout=0.3)
    except OSError:
        reachable = False
    else:
        connection.close()
        reachable = True
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    devices = []
    if request["gpu"]:
        query = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        devices = [
            [value.strip() for value in row] for row in csv.reader(query.stdout.splitlines())
        ]
    return {
        "event": "identity",
        "uid": os.getuid(),
        "root_write": root_write,
        "private_file_visible": Path(request["protected_path"]).exists(),
        "host_loopback_reachable": reachable,
        "gpu_nodes": sorted(p.name for p in Path("/dev").glob("nvidia*")),
        "devices": devices,
        "capabilities": status["CapEff"].strip(),
        "environment_names": sorted(os.environ),
        "sha256": hashlib.sha256(request["canary"].encode()).hexdigest(),
    }


def main():
    for line in sys.stdin:
        request = json.loads(line)
        operation = request["operation"]
        if operation == "identity":
            result = identity(request)
        elif operation == "hang":
            time.sleep(30)
            return
        elif operation == "started-hang":
            print('{"event":"busy"}', flush=True)
            time.sleep(30)
            return
        elif operation == "output":
            while True:
                sys.stdout.write("x" * 8192)
                sys.stdout.flush()
        elif operation == "busy":
            end = time.monotonic() + 3
            while time.monotonic() < end:
                pass
            result = {"event": "busy-finished"}
        elif operation == "memory":
            blocks = [bytearray(b"x" * (8 * 1024 * 1024)) for _index in range(80)]
            result = {"event": "memory-unlimited", "bytes": sum(map(len, blocks))}
        elif operation == "temporary-storage":
            filename = Path("/tmp/fixed-storage-check")
            count = 0
            try:
                with filename.open("wb") as stream:
                    for _index in range(40):
                        stream.write(b"x" * 1024 * 1024)
                        stream.flush()
                        count += 1
            except OSError as error:
                result = {"event": "storage", "limited": error.errno == errno.ENOSPC, "mib": count}
            else:
                result = {"event": "storage", "limited": False, "mib": count}
            finally:
                filename.unlink(missing_ok=True)
        else:
            raise ValueError("Unknown fixed infrastructure operation")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
