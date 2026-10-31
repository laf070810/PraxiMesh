"""Device-free NVRTC entry, executed only in the verified runsc guest."""

import ctypes as c
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.gpu_runtime import (  # noqa: E402
    NVRTC_LIBRARY,
    DriverFailure,
    bind,
    checked,
    compile_ptx,
    message,
)


def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        prepare = message()
        if prepare != {"protocol": 1, "operation": "prepare-compiler"}:
            raise DriverFailure(70)
        if list(Path("/dev").glob("nvidia*")):
            raise DriverFailure(70)
        try:
            c.CDLL("libcuda.so.1")
        except OSError:
            pass
        else:
            raise DriverFailure(70)
        library = c.CDLL(NVRTC_LIBRARY)
        major, minor = c.c_int(), c.c_int()
        checked(
            bind(library, "nvrtcVersion", [c.POINTER(c.c_int)] * 2),
            c.byref(major),
            c.byref(minor),
            platform=True,
        )
        if (major.value, minor.value) != (13, 1):
            raise DriverFailure(70)
        protocol.write(
            json.dumps(
                {
                    "event": "compiler-ready",
                    "nvrtc_version": [13, 1],
                    "gpu_nodes": [],
                    "cuda_driver_available": False,
                }
            )
            + "\n"
        )
        request = message()
        if request is None or set(request) != {"protocol", "source", "operation", "architecture"}:
            raise DriverFailure(70)
        ptx = compile_ptx(
            request["source"], request["operation"], request["architecture"], library=library
        )
        protocol.write(
            json.dumps(
                {
                    "event": "compiled",
                    "architecture": request["architecture"],
                    "ptx_bytes": c.sizeof(ptx),
                    "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(),
                }
            )
            + "\n"
        )
        if message() is not None:
            raise DriverFailure(70)
        return 0
    except DriverFailure as error:
        return error.exit_code
    except Exception:
        return 70
    finally:
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())
