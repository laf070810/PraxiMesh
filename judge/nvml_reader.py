"""Fixed NVML metadata subprocess: no CUDA calls, mutations or process names.

Function signatures and the v3 process ABI follow NVIDIA's NVML API and the
installed nvml.h. This module loads only the explicitly verified local NVML
snapshot, inside the trusted management container.
"""

import ctypes as C
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge.gpu_device_lock import GPU_UUID
from judge.nvml_artifacts import DIRECTORY, read_manifest, verify_bundle

MAX_PIDS = 4096
KINDS = {"compute": "Compute", "graphics": "Graphics", "mps": "MPSCompute"}


class ProcessInfo(C.Structure):
    _fields_ = [
        ("pid", C.c_uint),
        ("used_memory", C.c_ulonglong),
        ("gpu_instance", C.c_uint),
        ("compute_instance", C.c_uint),
    ]


class NVMLFailure(RuntimeError):
    pass


def function(library, name, arguments):
    call = getattr(library, name)
    call.argtypes, call.restype = arguments, C.c_int
    return call


def checked(call, *arguments):
    status = call(*arguments)
    if status != 0:
        # Only fixed API names and numeric errors; never raw process metadata.
        raise NVMLFailure(f"{call.__name__}: {status}")


def text_value(call, *arguments):
    buffer = C.create_string_buffer(256)
    checked(call, *arguments, buffer, C.c_uint(len(buffer)))
    if not buffer.value or len(buffer.value) == len(buffer):
        raise NVMLFailure("Invalid NVML text field")
    return buffer.value.decode("ascii", errors="strict")


def process_list(call, device):
    rows = (ProcessInfo * MAX_PIDS)()
    count = C.c_uint(MAX_PIDS)
    checked(call, device, C.byref(count), rows)
    if count.value > MAX_PIDS:
        raise NVMLFailure("NVML process inventory exceeded its bound")
    found = []
    for row in rows[: count.value]:
        if (
            not 0 < row.pid < 2**31
            or row.gpu_instance != 0xFFFFFFFF
            or row.compute_instance != 0xFFFFFFFF
        ):
            raise NVMLFailure("Expected a whole-device host process identity")
        found.append(row.pid)
    if len(set(found)) != len(found):
        raise NVMLFailure("Duplicate NVML process identity")
    return sorted(found)


def observe(device_uuid):
    if type(device_uuid) is not str or not GPU_UUID.fullmatch(device_uuid):
        raise ValueError("An explicit physical GPU UUID is required")
    if C.sizeof(ProcessInfo) != 24 or ProcessInfo.used_memory.offset != 8:
        raise NVMLFailure("Unsupported NVML process ABI")
    manifest = read_manifest()
    bundle_digest = verify_bundle(manifest)
    library = C.CDLL(str(DIRECTORY / manifest["library"]["name"]))
    pointer, uint_pointer = C.c_void_p, C.POINTER(C.c_uint)
    init = function(library, "nvmlInit_v2", [])
    shutdown = function(library, "nvmlShutdown", [])
    handle = function(library, "nvmlDeviceGetHandleByUUID", [C.c_char_p, C.POINTER(pointer)])
    strings = {
        "uuid": function(library, "nvmlDeviceGetUUID", [pointer, C.c_char_p, C.c_uint]),
        "model": function(library, "nvmlDeviceGetName", [pointer, C.c_char_p, C.c_uint]),
        "driver": function(library, "nvmlSystemGetDriverVersion", [C.c_char_p, C.c_uint]),
        "nvml_version": function(library, "nvmlSystemGetNVMLVersion", [C.c_char_p, C.c_uint]),
    }
    minor_number = function(library, "nvmlDeviceGetMinorNumber", [pointer, uint_pointer])
    architecture = function(
        library,
        "nvmlDeviceGetCudaComputeCapability",
        [pointer, C.POINTER(C.c_int), C.POINTER(C.c_int)],
    )
    queries = {
        kind: function(
            library,
            f"nvmlDeviceGet{name}RunningProcesses_v3",
            [pointer, uint_pointer, C.POINTER(ProcessInfo)],
        )
        for kind, name in KINDS.items()
    }
    checked(init)
    try:
        device = pointer()
        checked(handle, device_uuid.encode("ascii"), C.byref(device))
        if not device.value:
            raise NVMLFailure("Missing NVML device handle")

        def identity():
            minor, major_cc, minor_cc = C.c_uint(), C.c_int(), C.c_int()
            checked(minor_number, device, C.byref(minor))
            checked(architecture, device, C.byref(major_cc), C.byref(minor_cc))
            return {
                **{
                    key: text_value(call, *([device] if key in {"uuid", "model"} else []))
                    for key, call in strings.items()
                },
                "minor": minor.value,
                "architecture": [major_cc.value, minor_cc.value],
            }

        before = identity()
        if before["uuid"] != device_uuid or before["driver"] != manifest["driver_version"]:
            raise NVMLFailure("Management library or selected device mismatch")
        pids = {kind: process_list(call, device) for kind, call in queries.items()}
        if identity() != before:
            raise NVMLFailure("NVML device changed during observation")
        verify_bundle(manifest)
        return {"schema": 1, "bundle": bundle_digest, **before, "pids": pids}
    finally:
        checked(shutdown)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected one selected device")
        print(json.dumps(observe(sys.argv[1]), sort_keys=True))
    except Exception as error:
        print(
            str(error) if isinstance(error, NVMLFailure) else type(error).__name__, file=sys.stderr
        )
        raise SystemExit(1) from None
