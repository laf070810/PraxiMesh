import copy
import json

import pytest

from judge.gpu_contract import (
    GPUContractError,
    case_request,
    compile_request,
    input_shape,
    output_matches,
    project_public_problem,
    reference_values,
    validate_problem,
)

VECTOR = "vector-add-f32-v1"
MATRIX = "matmul-f32-v1"
COMPARISON = {"kind": "float", "absolute": 1e-5, "relative": 1e-5}


def problem():
    return {
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "entrypoint": "solve",
        "operation": VECTOR,
        "comparison": copy.deepcopy(COMPARISON),
        "tests": [
            {
                "name": "example",
                "public": True,
                "inputs": {"a": [1, 2], "b": [3, 4]},
                "expected": [4, 6],
            },
            {
                "name": "private-label",
                "public": False,
                "inputs": {"a": [9876], "b": [2234]},
                "expected": [12110],
            },
        ],
        "reference": "private-reference",
    }


def test_public_projection_and_runtime_requests_exclude_protected_fields():
    source = problem()
    validate_problem(source)
    public = project_public_problem(source)
    validate_problem(public, public_only=True)
    serialized = json.dumps(public)
    assert "private-label" not in serialized and "9876" not in serialized
    assert "private-reference" not in serialized and "12110" not in serialized
    request = case_request(VECTOR, source["tests"][1])
    assert set(request) == {"protocol", "inputs"}
    assert "expected" not in request and "name" not in request
    request["inputs"]["a"][0] = 0
    assert source["tests"][1]["inputs"]["a"] == [9876]


def test_scalar_oracles_cover_float32_rounding_rectangular_shapes_and_empty_vectors():
    assert reference_values(VECTOR, {"a": [0.1], "b": [0.2]}) == [0.30000001192092896]
    assert reference_values(VECTOR, {"a": [], "b": []}) == []
    inputs = {"m": 2, "n": 3, "k": 2, "a": [1, 2, 3, 4], "b": [5, 6, 7, 8, 9, 10]}
    assert reference_values(MATRIX, inputs) == [21, 24, 27, 47, 54, 61]
    assert input_shape(MATRIX, inputs) == (6, [1, 1, 1], [16, 16, 1])


@pytest.mark.parametrize(
    "inputs",
    [
        {"a": [1], "b": []},
        {"a": [True], "b": [0]},
        {"a": [float("nan")], "b": [0]},
        {"a": [1e20], "b": [0]},
        {"a": [0] * 4097, "b": [0] * 4097},
        {"a": [], "b": [], "compiler_flags": ["unsafe"]},
    ],
)
def test_vector_runtime_inputs_are_bounded_and_whitelisted(inputs):
    with pytest.raises(GPUContractError):
        input_shape(VECTOR, inputs)


@pytest.mark.parametrize("bad", [True, 0, 65, 1.5, "2"])
def test_matrix_dimensions_cannot_be_coerced_or_unbounded(bad):
    with pytest.raises(GPUContractError):
        input_shape(MATRIX, {"m": bad, "n": 1, "k": 1, "a": [1], "b": [2]})


@pytest.mark.parametrize(
    "output", [[True], [float("inf")], [None], [10**1000], [], {"value": 4}, [5]]
)
def test_nonfinite_malformed_or_wrong_outputs_never_pass(output):
    assert not output_matches(output, [4], COMPARISON)


def test_content_cannot_publish_inconsistent_expected_values_or_only_public_cases():
    source = problem()
    source["tests"][1]["expected"] = [7]
    with pytest.raises(GPUContractError):
        validate_problem(source)
    source = problem()
    source["tests"][1]["public"] = True
    with pytest.raises(GPUContractError):
        validate_problem(source)


@pytest.mark.parametrize("source", ["", "\0", "\ud800", "界" * 7000, None])
def test_source_limit_is_utf8_bytes_and_rejects_invalid_transport(source):
    with pytest.raises(GPUContractError):
        compile_request(source, VECTOR)


def test_device_source_is_only_projected_never_executed():
    source = 'extern "C" __global__ void solve() {}'
    assert compile_request(source, VECTOR) == {"protocol": 1, "source": source, "operation": VECTOR}
