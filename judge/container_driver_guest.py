"""Fixed no-device driver load/compile probe, executed only inside runsc."""

import ctypes as c
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.driver_artifacts import verify_bundle  # noqa: E402
from judge.gpu_runtime import DriverFailure, compile_ptx, message  # noqa: E402


def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        prepare = message()
        if type(prepare) is not dict or set(prepare) != {"protocol", "operation", "manifest"}:
            raise DriverFailure(70)
        if prepare["operation"] != "prepare-driver" or list(Path("/dev").glob("nvidia*")):
            raise DriverFailure(70)
        digest = verify_bundle(Path("/opt/driver"), prepare["manifest"])
        loaded = []
        handles = []
        for info in prepare["manifest"]["libraries"].values():
            # Load dependencies and resolve symbols only. No cuInit or CUDA context.
            handles.append(c.CDLL(info["soname"]))
            loaded.append(info["soname"])
        driver = c.CDLL("libcuda.so.1")
        for symbol in ("cuInit", "cuDeviceGetCount", "cuModuleLoadDataEx", "cuLaunchKernel"):
            getattr(driver, symbol)
        protocol.write(
            json.dumps(
                {
                    "event": "driver-loaded",
                    "manifest_sha256": digest,
                    "libraries": sorted(loaded),
                    "gpu_nodes": [],
                    "cuda_initialized": False,
                }
            )
            + "\n"
        )
        request = message()
        if request is None:
            return 0
        if set(request) != {"protocol", "source", "operation", "architecture"}:
            raise DriverFailure(70)
        ptx = compile_ptx(request["source"], request["operation"], request["architecture"])
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
