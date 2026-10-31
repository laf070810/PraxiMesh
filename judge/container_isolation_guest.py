"""Fixed resource probe; invoked only in the pinned nested runsc guest."""

import errno
import hashlib
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path


def cuda_identity():
    # The explicit CPU fixture never calls this function or loads a driver.
    import ctypes as c
    import uuid

    cuda = c.CDLL("libcuda.so.1")
    intp = c.POINTER(c.c_int)

    def call(name, types, *args):
        function = getattr(cuda, name)
        function.argtypes, function.restype = types, c.c_int
        if function(*args) != 0:
            raise RuntimeError("Fixed CUDA identity probe failed")

    call("cuInit", [c.c_uint], 0)
    count, device, major, minor = (c.c_int() for _ in range(4))
    call("cuDeviceGetCount", [intp], c.byref(count))
    if count.value != 1:
        raise RuntimeError("Expected exactly one visible CUDA device")
    call("cuDeviceGet", [intp, c.c_int], c.byref(device), 0)
    identifier, model = (c.c_ubyte * 16)(), c.create_string_buffer(256)
    call("cuDeviceGetUuid_v2", [c.c_void_p, c.c_int], identifier, device)
    call("cuDeviceGetName", [c.c_void_p, c.c_int, c.c_int], model, len(model), device)
    call("cuDeviceComputeCapability", [intp, intp, c.c_int], c.byref(major), c.byref(minor), device)
    return {
        "uuid": "GPU-" + str(uuid.UUID(bytes=bytes(identifier))),
        "model": model.value.decode("ascii"),
        "architecture": [major.value, minor.value],
        "count": count.value,
    }


def identity(request):
    if type(request["gpu"]) is not bool:
        raise ValueError("Explicit resource probe device mode is required")
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
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    devices = []
    for path in sorted(Path("/dev").glob("nvidia*")):
        info = path.lstat()
        devices.append(
            {
                "path": str(path),
                "character": stat.S_ISCHR(info.st_mode),
                "major": os.major(info.st_rdev),
                "minor": os.minor(info.st_rdev),
            }
        )
    return {
        "event": "identity",
        "uid": os.getuid(),
        "root_write": root_write,
        "private_file_visible": Path(request["protected_path"]).exists(),
        "controller_loopback_reachable": reachable,
        "gpu_nodes": devices,
        "cuda": cuda_identity() if request["gpu"] else None,
        "capabilities": status["CapEff"].strip(),
        "no_new_privileges": status["NoNewPrivs"].strip(),
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
            blocks = [bytearray(b"x" * (8 * 1024 * 1024)) for _ in range(80)]
            result = {"event": "memory-unlimited", "bytes": sum(map(len, blocks))}
        elif operation == "temporary-storage":
            filename = Path("/tmp/fixed-storage-check")
            count = 0
            try:
                with filename.open("wb") as stream:
                    for _ in range(40):
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
