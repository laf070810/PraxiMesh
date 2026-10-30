"""Reviewed CUDA fixtures; execution belongs exclusively to the GPU sandbox."""

VECTOR_REFERENCE = r"""
extern "C" __global__ void solve(const float* a, const float* b, float* out, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = a[i] + b[i];
}
"""

MATRIX_REFERENCE = r"""
extern "C" __global__ void solve(const float* a, const float* b, float* out,
                                int m, int n, int k) {
    int column = blockIdx.x * blockDim.x + threadIdx.x;
    int row = blockIdx.y * blockDim.y + threadIdx.y;
    if (row < m && column < n) {
        float value = 0;
        for (int i = 0; i < k; ++i) value += a[row * k + i] * b[i * n + column];
        out[row * n + column] = value;
    }
}
"""


def correctness_fixtures():
    vector_inputs = [
        {"a": [], "b": []},
        {"a": [0.1, -4, 0, 5.75], "b": [0.2, 7, -2, -0.75]},
        {"a": [float(i % 13 - 6) for i in range(257)], "b": [0.25] * 257},
    ]

    matrix_inputs = [
        {"m": 2, "n": 3, "k": 2, "a": [1, 2, 3, 4], "b": [5, 6, 7, 8, 9, 10]},
        {
            "m": 17,
            "n": 19,
            "k": 7,
            "a": [float(i % 11 - 5) / 4 for i in range(17 * 7)],
            "b": [float(i % 7 - 3) / 2 for i in range(7 * 19)],
        },
    ]
    return [
        {
            "name": "vector-reference",
            "operation": "vector-add-f32-v1",
            "source": VECTOR_REFERENCE,
            "inputs": vector_inputs,
            "exit_code": 0,
            "correct": True,
        },
        {
            "name": "vector-subtraction",
            "operation": "vector-add-f32-v1",
            "source": VECTOR_REFERENCE.replace("a[i] + b[i]", "a[i] - b[i]"),
            "inputs": vector_inputs[1:],
            "exit_code": 0,
            "correct": False,
        },
        {
            "name": "vector-unwritten",
            "operation": "vector-add-f32-v1",
            "source": VECTOR_REFERENCE.replace("out[i] = a[i] + b[i]", "(void)i"),
            "inputs": vector_inputs[1:],
            "exit_code": 0,
            "correct": False,
        },
        {
            "name": "matrix-reference",
            "operation": "matmul-f32-v1",
            "source": MATRIX_REFERENCE,
            "inputs": matrix_inputs,
            "exit_code": 0,
            "correct": True,
        },
        {
            "name": "matrix-wrong-stride",
            "operation": "matmul-f32-v1",
            "source": MATRIX_REFERENCE.replace("b[i * n + column]", "b[column * k + i]"),
            "inputs": matrix_inputs,
            "exit_code": 0,
            "correct": False,
        },
        {
            "name": "compile-error",
            "operation": "vector-add-f32-v1",
            "source": "not valid CUDA C++",
            "inputs": [],
            "exit_code": 3,
            "correct": False,
        },
        {
            "name": "missing-entrypoint",
            "operation": "vector-add-f32-v1",
            "source": VECTOR_REFERENCE.replace("void solve(", "void another("),
            "inputs": [],
            "exit_code": 2,
            "correct": False,
        },
    ]


def fault_fixtures():
    """Faults run serially on an explicitly dedicated card, followed by recovery."""
    inputs = [{"a": [1, -2, 0.5], "b": [3, 5, -0.25]}]

    def fixture(name, source, *, exit_code=0, compiled=False, **extra):
        return {
            "name": name,
            "operation": "vector-add-f32-v1",
            "source": source,
            "inputs": inputs,
            "exit_code": exit_code,
            "correct": exit_code == 0,
            "compiled": compiled,
            **extra,
        }

    spoof = VECTOR_REFERENCE.replace(
        "if (i < n)",
        'if (i == 0) printf("{\\"values\\":[999]}\\n");\n    if (i < n)',
    )
    heap = r"""
extern "C" __device__ void *malloc(unsigned long);
extern "C" __device__ void free(void*);
extern "C" __global__ void solve(const float* a, const float* b, float* out, int n) {
    if (blockIdx.x || threadIdx.x) return;
    void* small = malloc(4UL * 1024 * 1024);
    void* p = malloc(16UL * 1024 * 1024);
    for (int i = 0; i < n; ++i) out[i] = small && !p ? a[i] + b[i] : 999;
    if (small) free(small);
    if (p) free(p);
}
"""
    illegal = VECTOR_REFERENCE.replace("out[i] = a[i] + b[i]", "*((volatile float*)0) = 1")
    oversized = (
        "__device__ volatile char oversized[32ULL * 1024 * 1024 * 1024];\n"
        + VECTOR_REFERENCE.replace("out[i] = a[i] + b[i]", "out[i] = oversized[i]")
    )
    invalid_ptx = VECTOR_REFERENCE.replace("out[i] = a[i] + b[i]", 'asm volatile("nop;")')
    long_running = VECTOR_REFERENCE.replace(
        "out[i] = a[i] + b[i]",
        "{ unsigned long long start = clock64(); "
        "while (clock64() - start < 60000000000ULL) {} out[i] = a[i] + b[i]; }",
    )
    return [
        fixture("printf-spoof", spoof),
        fixture("device-heap-bound", heap),
        fixture("illegal-address", illegal, exit_code=2, compiled=True),
        fixture("recover-illegal-address", VECTOR_REFERENCE),
        fixture("module-memory-limit", oversized, exit_code=4),
        fixture("recover-module-memory", VECTOR_REFERENCE),
        fixture("invalid-ptx", invalid_ptx, exit_code=2),
        fixture(
            "kernel-timeout",
            long_running,
            exit_code=137,
            compiled=True,
            expected_timeout=True,
            timeout=8,
        ),
        fixture("recover-kernel-timeout", VECTOR_REFERENCE),
    ]
