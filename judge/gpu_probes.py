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
