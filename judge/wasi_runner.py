"""Trusted child driver. User code runs exclusively inside fixed Wasm interpreters.

This executable receives only the source/current arguments, never expected
answers or database credentials. Guest stdout is data, not this host protocol.
"""

import ctypes
import json
import os
import resource
import signal
import sys
import tempfile
from pathlib import Path

# -I intentionally excludes the script directory; add only our trusted package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge.sandbox import GUEST  # noqa: E402
from judge.stdio import COMPILE_GUEST, PROGRAM_GUEST  # noqa: E402
from judge.wasi_policy import (  # noqa: E402
    FUEL,
    HOST_CPU_SECONDS,
    HOST_MEMORY,
    INPUT,
    JAVASCRIPT_FUEL,
    MEMORY,
    OUTPUT,
    PYTHON_VERSION,
    WALL_SECONDS,
    engine_config,
)


def main():
    sys.dont_write_bytecode = True
    # Kill the driver if its supervisor dies, including the registration race.
    parent_pid = int(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        return 70
    # An OS default signal also terminates blocking WASI clock hostcalls.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(WALL_SECONDS)
    # Bound native runtime pools before initialization, including on hosts with
    # hundreds of CPUs. These are host controls, never inherited by the guest.
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    os.environ["TOKIO_WORKER_THREADS"] = "1"
    os.environ["RAYON_NUM_THREADS"] = "1"
    resource.setrlimit(resource.RLIMIT_AS, (HOST_MEMORY, HOST_MEMORY))
    resource.setrlimit(resource.RLIMIT_CPU, (HOST_CPU_SECONDS, HOST_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (INPUT, INPUT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import wasmtime

    bundle = Path(sys.argv[1])
    language = sys.argv[3]
    if language not in {"python", "javascript", "python-compile", "python-stdio"}:
        return 70
    module_name = "quickjs.cwasm" if language == "javascript" else "python.cwasm"
    request = sys.stdin.buffer.read(INPUT + 1)
    if len(request) > INPUT:
        return 70
    output = bytearray()
    total = 0

    def capture(data, keep):
        nonlocal total
        total += len(data)
        if total > OUTPUT:
            # A host exit; guest proc_exit is caught separately below and cannot
            # forge this driver status. Never buffer unbounded guest output.
            os._exit(72)
        if keep:
            output.extend(data)
        return len(data)

    guest_input = request
    program_source = None
    if language == "python-stdio":
        program = json.loads(request)
        if set(program) != {"source", "stdin"} or not all(
            isinstance(value, str) for value in program.values()
        ):
            return 70
        program_source = program["source"]
        guest_input = program["stdin"].encode("utf-8")
    with tempfile.TemporaryFile() as stdin:
        stdin.write(guest_input)
        stdin.seek(0)
        with wasmtime.Engine(engine_config()) as engine:
            # This native cache is produced from the fixed interpreter by our
            # trusted build, included in the pinned manifest, and verified by
            # the parent. NEVER deserialize a user-provided artifact.
            with wasmtime.Module.deserialize_file(engine, str(bundle / module_name)) as module:
                with wasmtime.Store(engine) as store:
                    store.set_limits(
                        memory_size=MEMORY, table_elements=20000, instances=1, tables=1, memories=1
                    )
                    store.set_fuel(JAVASCRIPT_FUEL if language == "javascript" else FUEL)
                    wasi = wasmtime.WasiConfig()
                    wasi.env = []
                    if language != "javascript":
                        wrapper = {
                            "python": GUEST,
                            "python-compile": COMPILE_GUEST,
                            "python-stdio": PROGRAM_GUEST,
                        }[language]
                        wasi.argv = ["/python/bin/python3", "-I", "-B", "-c", wrapper] + (
                            [program_source] if program_source is not None else []
                        )
                        wasi.preopen_dir(str(bundle / "stdlib"), "/python/lib", fs_mutable=False)
                        # CPython searches this fixed directory at startup. Map
                        # only the same already-readable library bytes, read-only,
                        # so its missing-path warning does not consume user output.
                        version = ".".join(PYTHON_VERSION.split(".")[:2])
                        wasi.preopen_dir(
                            str(bundle / "stdlib"),
                            f"/python/lib/python{version}/lib-dynload",
                            fs_mutable=False,
                        )
                    else:
                        wasi.argv = ["quickjs-function"]
                    wasi.stdin_file = f"/proc/self/fd/{stdin.fileno()}"
                    wasi.stdout_custom = lambda data: capture(data, True)
                    wasi.stderr_custom = lambda data: capture(data, False)
                    store.set_wasi(wasi)
                    with wasmtime.Linker(engine) as linker:
                        linker.define_wasi()
                        instance = linker.instantiate(store, module)
                        os.write(2, b"R")  # Trusted readiness, never guest stderr.
                        try:
                            instance.exports(store)["_start"](store)
                        except wasmtime.ExitTrap as exc:
                            if exc.code:
                                if language == "python-stdio":
                                    return 75  # ALL nonzero learner exits are runtime errors.
                                if language == "python-compile":
                                    return {3: 73, 4: 74}.get(exc.code, 70)
                                if language == "javascript":
                                    return {3: 73, 4: 74, 5: 72, 70: 70}.get(exc.code, 75)
                                return {3: 73, 4: 74}.get(exc.code, 75)
                        except wasmtime.Trap as exc:
                            if exc.trap_code == wasmtime.TrapCode.OUT_OF_FUEL:
                                return 71
                            return 75
    # No trusted verdict comes from the guest; the controller validates and
    # compares this bounded raw return-value envelope.
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        # Runtime/asset/transport failures are platform failures. Never expose
        # raw stderr (which can include private input) or call them wrong answers.
        code = 70
    # Native WASI callbacks may still own Python references on runtime threads.
    # The driver owns no persistent state; flush the bounded protocol, then end
    # the entire process before interpreter-finalization can race those threads.
    sys.stdout.buffer.flush()
    os._exit(code)
