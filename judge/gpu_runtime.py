"""Trusted CUDA device-only driver; run ONLY inside the verified GPU sandbox.

NVRTC compiles learner device source here. The host controller retains expected
values and measures elapsed time. No learner host code is linked or executed.
"""

import ctypes as c
import json
import math
import os
import sys
import traceback

from judge.gpu_contract import ENTRYPOINT, PROTOCOL, compile_request, input_shape

NVRTC_LIBRARY = "/opt/nvrtc/libnvrtc.so.13.1.115"
MAX_MESSAGE = 128 * 1024
MAX_PTX = 4 * 1024 * 1024


class DriverFailure(Exception):
    def __init__(self, exit_code, operation=None, native_code=None):
        self.exit_code = exit_code
        self.operation = operation
        self.native_code = native_code


def bind(library, name, args):
    function = getattr(library, name)
    function.restype = c.c_int
    function.argtypes = args
    return function


def checked(function, *args, platform=False):
    code = function(*args)
    if code:
        raise DriverFailure(70 if platform else 4 if code == 2 else 2, function.__name__, code)


def message():
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if not raw:
        return None
    if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
        raise DriverFailure(70)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DriverFailure(70)
            result[key] = value
        return result

    def constant(_value):
        raise DriverFailure(70)

    result = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if (
        type(result) is not dict
        or type(result.get("protocol")) is not int
        or result["protocol"] != PROTOCOL
    ):
        raise DriverFailure(70)
    return result


class Kernel:
    def __init__(self, source, operation):
        compile_request(source, operation)
        self.operation = operation
        self.cuda = c.CDLL("libcuda.so.1")
        self.nvrtc = c.CDLL(NVRTC_LIBRARY)
        self.context = c.c_void_p()
        self.module = c.c_void_p()
        self.function = c.c_void_p()
        self._bind()
        checked(self.init, 0, platform=True)
        count = c.c_int()
        checked(self.device_count, c.byref(count), platform=True)
        if count.value != 1:
            raise DriverFailure(70, "device_count", count.value)
        major, minor = c.c_int(), c.c_int()
        checked(self.capability, c.byref(major), c.byref(minor), 0, platform=True)
        self.arch = (major.value, minor.value)
        checked(self.create_context, c.byref(self.context), 0, 0, platform=True)
        # Bound the device malloc heap and printf FIFO. Host allocations use
        # device-only memory; no host buffer is mapped into the learner kernel.
        checked(self.set_limit, 2, 8 * 1024 * 1024, platform=True)
        checked(self.set_limit, 1, 16 * 1024, platform=True)
        ptx = self._compile(source)
        checked(self.load_module, c.byref(self.module), ptx)
        checked(self.get_function, c.byref(self.function), self.module, ENTRYPOINT.encode())

    def _bind(self):
        ptr = c.c_void_p
        intp = c.POINTER(c.c_int)
        ptrp = c.POINTER(ptr)
        dev = c.c_uint64
        self.init = bind(self.cuda, "cuInit", [c.c_uint])
        self.device_count = bind(self.cuda, "cuDeviceGetCount", [intp])
        self.capability = bind(self.cuda, "cuDeviceComputeCapability", [intp, intp, c.c_int])
        self.create_context = bind(self.cuda, "cuCtxCreate_v2", [ptrp, c.c_uint, c.c_int])
        self.destroy_context = bind(self.cuda, "cuCtxDestroy_v2", [ptr])
        self.set_limit = bind(self.cuda, "cuCtxSetLimit", [c.c_int, c.c_size_t])
        self.load_module = bind(self.cuda, "cuModuleLoadData", [ptrp, ptr])
        self.get_function = bind(self.cuda, "cuModuleGetFunction", [ptrp, ptr, c.c_char_p])
        self.allocate = bind(self.cuda, "cuMemAlloc_v2", [c.POINTER(dev), c.c_size_t])
        self.free = bind(self.cuda, "cuMemFree_v2", [dev])
        self.to_device = bind(self.cuda, "cuMemcpyHtoD_v2", [dev, ptr, c.c_size_t])
        self.to_host = bind(self.cuda, "cuMemcpyDtoH_v2", [ptr, dev, c.c_size_t])
        self.fill = bind(self.cuda, "cuMemsetD32_v2", [dev, c.c_uint, c.c_size_t])
        self.launch = bind(self.cuda, "cuLaunchKernel", [ptr, *([c.c_uint] * 7), ptr, ptrp, ptrp])
        self.sync = bind(self.cuda, "cuCtxSynchronize", [])

    def _compile(self, source):
        program = c.c_void_p()
        create = bind(
            self.nvrtc,
            "nvrtcCreateProgram",
            [c.POINTER(c.c_void_p), c.c_char_p, c.c_char_p, c.c_int, c.c_void_p, c.c_void_p],
        )
        compile_program = bind(
            self.nvrtc, "nvrtcCompileProgram", [c.c_void_p, c.c_int, c.POINTER(c.c_char_p)]
        )
        destroy = bind(self.nvrtc, "nvrtcDestroyProgram", [c.POINTER(c.c_void_p)])
        size_fn = bind(self.nvrtc, "nvrtcGetPTXSize", [c.c_void_p, c.POINTER(c.c_size_t)])
        ptx_fn = bind(self.nvrtc, "nvrtcGetPTX", [c.c_void_p, c.c_void_p])
        checked(
            create, c.byref(program), source.encode(), b"solution.cu", 0, None, None, platform=True
        )
        try:
            options = [
                f"--gpu-architecture=compute_{self.arch[0]}{self.arch[1]}".encode(),
                b"--std=c++17",
                b"--fmad=false",
                b"--relocatable-device-code=false",
            ]
            argv = (c.c_char_p * len(options))(*options)
            result = compile_program(program, len(options), argv)
            if result:
                if os.environ.get("PRAXIMESH_GPU_DIAGNOSTICS") == "1":
                    log_size = c.c_size_t()
                    get_size = bind(
                        self.nvrtc, "nvrtcGetProgramLogSize", [c.c_void_p, c.POINTER(c.c_size_t)]
                    )
                    get_log = bind(self.nvrtc, "nvrtcGetProgramLog", [c.c_void_p, c.c_void_p])
                    if get_size(program, c.byref(log_size)) == 0 and 0 < log_size.value <= 16384:
                        log = c.create_string_buffer(log_size.value)
                        if get_log(program, log) == 0:
                            print(log.value.decode(errors="replace"), file=sys.stderr)
                raise DriverFailure(3 if result == 6 else 70, "nvrtcCompileProgram", result)
            size = c.c_size_t()
            checked(size_fn, program, c.byref(size), platform=True)
            if not 1 <= size.value <= MAX_PTX:
                raise DriverFailure(4)
            ptx = c.create_string_buffer(size.value)
            checked(ptx_fn, program, ptx, platform=True)
            return ptx
        finally:
            destroy(c.byref(program))

    def execute(self, inputs):
        length, grid, block = input_shape(self.operation, inputs)
        allocations = []

        def allocate(values=None, count=None):
            count = len(values) if values is not None else count
            pointer = c.c_uint64()
            checked(self.allocate, c.byref(pointer), max(1, count) * 4)
            allocations.append(pointer)
            if values:
                array = (c.c_float * count)(*values)
                checked(self.to_device, pointer, array, count * 4)
            return pointer

        try:
            a, b = (allocate(inputs[key]) for key in ("a", "b"))
            output = allocate(count=length)
            # Unwritten output must fail correctness, including repeated cases.
            checked(self.fill, output, 0x7FC00000, max(1, length))
            scalar_values = (
                [length]
                if self.operation == "vector-add-f32-v1"
                else [inputs[key] for key in ("m", "n", "k")]
            )
            parameters = [a, b, output, *(c.c_int(v) for v in scalar_values)]
            arguments = (c.c_void_p * len(parameters))(
                *(c.cast(c.byref(value), c.c_void_p) for value in parameters)
            )
            checked(self.launch, self.function, *grid, *block, 0, None, arguments, None)
            checked(self.sync)
            values = (c.c_float * length)()
            if length:
                checked(self.to_host, values, output, length * 4)
            return [v if math.isfinite(v) else None for v in values]
        finally:
            for pointer in reversed(allocations):
                self.free(pointer)

    def close(self):
        if self.context:
            self.destroy_context(self.context)


def main():
    # Device printf uses stdout. It must not be able to inject protocol frames.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    kernel = None
    try:
        request = message()
        if request is None or set(request) != {"protocol", "source", "operation"}:
            raise DriverFailure(70)
        kernel = Kernel(request["source"], request["operation"])
        protocol.write(json.dumps({"event": "compiled", "architecture": list(kernel.arch)}) + "\n")
        while (request := message()) is not None:
            if set(request) != {"protocol", "inputs"}:
                raise DriverFailure(70)
            values = kernel.execute(request["inputs"])
            protocol.write(json.dumps({"values": values}, allow_nan=False) + "\n")
        return 0
    except DriverFailure as error:
        if os.environ.get("PRAXIMESH_GPU_DIAGNOSTICS") == "1":
            print(
                json.dumps(
                    {
                        "operation": error.operation,
                        "native_code": error.native_code,
                        "exit_code": error.exit_code,
                    }
                ),
                file=sys.stderr,
            )
        return error.exit_code
    except Exception:
        # No raw source, compiler logs, inputs or exception strings on protocol.
        if os.environ.get("PRAXIMESH_GPU_DIAGNOSTICS") == "1":
            traceback.print_exc(file=sys.stderr)
        return 70
    finally:
        if kernel is not None:
            kernel.close()
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())
