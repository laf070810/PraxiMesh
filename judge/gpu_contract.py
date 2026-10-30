"""Bounded CUDA device-kernel inputs and controller-owned correctness checks.

This is not an execution backend and does not enable GPU submission admission.
Expected values never appear in a runtime request.
"""

import copy
import math
import struct

from judge.results import matches

EVALUATOR = "cuda-kernel-v1"
LANGUAGE = "cuda"
PROTOCOL = 1
ENTRYPOINT = "solve"
OPERATIONS = frozenset({"vector-add-f32-v1", "matmul-f32-v1"})
SOURCE_BYTES = 20_000
MAX_VECTOR = 4096
MAX_DIMENSION = 64
MAX_NUMBER = 10_000
MAX_CASES = 8
MAX_TOLERANCE = 0.01


class GPUContractError(ValueError):
    pass


def invalid():
    return GPUContractError("Invalid CUDA kernel evaluation contract")


def f32(value):
    if type(value) not in {int, float}:
        raise invalid()
    try:
        value = float(value)
        if not math.isfinite(value):
            raise invalid()
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        raise invalid() from None


def _array(value, length):
    if type(value) is not list or len(value) != length:
        raise invalid()
    for item in value:
        if type(item) not in {int, float} or abs(item) > MAX_NUMBER or not math.isfinite(item):
            raise invalid()
        f32(item)


def input_shape(operation, inputs):
    """Return (output length, grid, block), rejecting every unknown input field."""
    if type(operation) is not str or operation not in OPERATIONS or type(inputs) is not dict:
        raise invalid()
    if operation == "vector-add-f32-v1":
        if set(inputs) != {"a", "b"} or type(inputs["a"]) is not list:
            raise invalid()
        n = len(inputs["a"])
        if not 0 <= n <= MAX_VECTOR:
            raise invalid()
        _array(inputs["a"], n)
        _array(inputs["b"], n)
        return n, [max(1, (n + 255) // 256), 1, 1], [256, 1, 1]
    if set(inputs) != {"a", "b", "m", "n", "k"}:
        raise invalid()
    m, n, k = (inputs[key] for key in ("m", "n", "k"))
    if any(type(v) is not int or not 1 <= v <= MAX_DIMENSION for v in (m, n, k)):
        raise invalid()
    _array(inputs["a"], m * k)
    _array(inputs["b"], k * n)
    return m * n, [(n + 15) // 16, (m + 15) // 16, 1], [16, 16, 1]


def reference_values(operation, inputs):
    """Small correctness oracle; no submitted code is imported or executed."""
    input_shape(operation, inputs)
    a, b = ([f32(v) for v in inputs[key]] for key in ("a", "b"))
    if operation == "vector-add-f32-v1":
        return [f32(x + y) for x, y in zip(a, b, strict=True)]
    m, n, k = (inputs[key] for key in ("m", "n", "k"))
    output = []
    for row in range(m):
        for column in range(n):
            value = 0.0
            for inner in range(k):
                value = f32(value + f32(a[row * k + inner] * b[inner * n + column]))
            output.append(value)
    return output


def validate_comparison(comparison):
    if type(comparison) is not dict or set(comparison) != {"kind", "absolute", "relative"}:
        raise invalid()
    if comparison["kind"] != "float":
        raise invalid()
    for key in ("absolute", "relative"):
        value = comparison[key]
        if type(value) not in {int, float} or not 0 <= value <= MAX_TOLERANCE:
            raise invalid()


def validate_problem(problem, *, public_only=False):
    if (
        type(problem) is not dict
        or problem.get("evaluator") != EVALUATOR
        or problem.get("language") != LANGUAGE
        or type(problem.get("operation")) is not str
        or problem["operation"] not in OPERATIONS
        or problem.get("entrypoint") != ENTRYPOINT
        or type(problem.get("tests")) is not list
        or not (1 if public_only else 2) <= len(problem["tests"]) <= MAX_CASES
    ):
        raise invalid()
    validate_comparison(problem.get("comparison"))
    names, visible = set(), set()
    for case in problem["tests"]:
        if type(case) is not dict or set(case) != {"name", "public", "inputs", "expected"}:
            raise invalid()
        name = case["name"]
        if type(name) is not str or not 1 <= len(name) <= 80 or name in names:
            raise invalid()
        if type(case["public"]) is not bool or (public_only and not case["public"]):
            raise invalid()
        expected = reference_values(problem["operation"], case["inputs"])
        if not output_matches(case["expected"], expected, problem["comparison"]):
            raise invalid()
        names.add(name)
        visible.add(case["public"])
    if visible != ({True} if public_only else {True, False}):
        raise invalid()


def output_matches(actual, expected, comparison):
    validate_comparison(comparison)
    if type(actual) is not list or len(actual) != len(expected):
        return False
    try:
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in actual):
            return False
    except OverflowError:
        return False
    return matches(actual, expected, comparison)


def compile_request(source, operation):
    if type(source) is not str or type(operation) is not str or operation not in OPERATIONS:
        raise invalid()
    try:
        size = len(source.encode("utf-8"))
    except UnicodeError:
        raise invalid() from None
    if not 1 <= size <= SOURCE_BYTES or "\0" in source:
        raise invalid()
    return {"protocol": PROTOCOL, "operation": operation, "source": source}


def case_request(operation, case):
    """Whitelist projection: omit expected output, visibility and hidden labels."""
    input_shape(operation, case["inputs"])
    return {"protocol": PROTOCOL, "inputs": copy.deepcopy(case["inputs"])}


def project_public_problem(problem):
    validate_problem(problem)
    return {
        "evaluator": EVALUATOR,
        "language": LANGUAGE,
        "operation": problem["operation"],
        "entrypoint": ENTRYPOINT,
        "comparison": copy.deepcopy(problem["comparison"]),
        "tests": copy.deepcopy([case for case in problem["tests"] if case["public"]]),
    }
