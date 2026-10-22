"""Fixed capabilities and budgets for the reviewed Python and JavaScript WASI artifact."""

POLICY_VERSION = "wasi-functions-v2"
QUICKJS_VERSION = "2026-06-04"
LANGUAGES = ("python", "javascript")
PYTHON_VERSION = "3.14.9"
WASMTIME_VERSION = "50.0.1"
TARGET = "x86_64-unknown-linux-gnu"
PYTHON_BUNDLE_FILES = (
    "python.wasm",
    "python.cwasm",
    "stdlib/python314.zip",
    "LICENSE.python",
    "THIRD_PARTY_NOTICES.txt",
)
BUNDLE_FILES = PYTHON_BUNDLE_FILES + (
    "quickjs.wasm",
    "quickjs.cwasm",
    "LICENSE.quickjs",
)
JAVASCRIPT_FUEL = 100_000_000
MEMORY = 128 * 1024 * 1024
OUTPUT = 32 * 1024
INPUT = 256000
FUEL = 5_000_000_000
WALL_SECONDS = 15
HOST_MEMORY = 1536 * 1024 * 1024
HOST_CPU_SECONDS = 10


def engine_config():
    import wasmtime

    config = wasmtime.Config()
    # An explicit target disables build-host CPU feature inference. The only
    # accepted guest module is our trusted interpreter core-Wasm artifacts.
    config.target = TARGET
    config.wasm_component_model = False
    config.consume_fuel = True
    config.wasm_threads = False
    config.wasm_memory64 = False
    config.wasm_multi_memory = False
    config.parallel_compilation = False
    config.memory_init_cow = False
    config.max_wasm_stack = 1024 * 1024
    config.memory_reservation = MEMORY
    config.memory_guard_size = 65536
    config.memory_reservation_for_growth = 16 * 1024 * 1024
    return config
