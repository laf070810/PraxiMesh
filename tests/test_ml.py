"""Protocol/host-scoring tests; recording doubles do not claim sandbox execution."""

import copy
import json

import pytest

from judge.ml import (
    MLContractError,
    dataset_digest,
    evaluate_public,
    load_problem,
    model_metrics,
    project_public_problem,
    python_ml_linear_v1,
    validate_problem,
    validate_public_problem,
)
from judge.sandbox import Execution, SandboxUnavailable


@pytest.fixture
def problem():
    cases = []
    for public, offset, name in [(True, 0, "public"), (False, 20, "secret-scenario")]:
        case = {
            "name": name,
            "public": public,
            "train": {
                "features": [[offset + x] for x in range(4)],
                "targets": [2 * (offset + x) + 3 for x in range(4)],
            },
            "holdout": {
                "features": [[offset + x] for x in range(4, 8)],
                "targets": [2 * (offset + x) + 3 for x in range(4, 8)],
            },
            "thresholds": {"mse": 0, "mae": 0},
            "dataset": {"id": name, "revision": 1, "seed": offset, "digest": ""},
        }
        case["dataset"]["digest"] = dataset_digest(case)
        cases.append(case)
    return {
        "evaluator": "python-ml-linear-v1",
        "language": "python",
        "function": "train",
        "model_kind": "linear-regression-v1",
        "tests": cases,
    }


class RecordingSandbox:
    def __init__(self, *results):
        self.results = iter(results)
        self.requests = []

    def execute(self, source, function, args):
        self.requests.append((source, function, copy.deepcopy(args)))
        return next(self.results)


def execution(model=None, verdict="ok", elapsed=7):
    return Execution(verdict, model, elapsed)


def rehash(problem):
    for case in problem["tests"]:
        case["dataset"]["digest"] = dataset_digest(case)


def test_actual_host_metrics_have_manual_oracle_and_exact_model_schema():
    holdout = {"features": [[1], [2], [3], [4]], "targets": [2, 3, 4, 5]}
    assert model_metrics({"weights": [2], "bias": 0}, holdout) == {"mse": 3.5, "mae": 1.5}
    assert model_metrics({"weights": [1], "bias": 1}, holdout) == {"mse": 0, "mae": 0}
    assert model_metrics({"weights": [1], "bias": 1, "mse": 0}, holdout) is None


def test_submit_sends_only_training_data_and_publishes_only_public_host_metrics(problem):
    sandbox = RecordingSandbox(*[execution({"weights": [2], "bias": 3}) for _ in range(2)])
    result = python_ml_linear_v1(problem, "learner source", "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == result["total"] == 2
    assert result["elapsed_ms"] == 14
    assert sandbox.requests == [
        ("learner source", "train", [case["train"]["features"], case["train"]["targets"]])
        for case in problem["tests"]
    ]
    assert result["public_cases"] == [
        {
            "name": "public",
            "verdict": "accepted",
            "label": "通过",
            "metrics": {"mse": 0, "mae": 0},
            "thresholds": {"mse": 0, "mae": 0},
        }
    ]
    assert set(result) == {
        "verdict",
        "label",
        "passed",
        "total",
        "elapsed_ms",
        "public_cases",
        "assessment",
    }
    serialized = json.dumps(result)
    for forbidden in ("secret-scenario", "weights", "bias", "seed", "holdout", "targets"):
        assert forbidden not in serialized
    for case in problem["tests"]:
        assert case["dataset"]["digest"] not in serialized


def test_run_executes_only_public_cases(problem):
    sandbox = RecordingSandbox(execution({"weights": [2], "bias": 3}))
    result = python_ml_linear_v1(problem, "source", "run", sandbox)
    assert result["passed"] == result["total"] == 1
    assert len(sandbox.requests) == 1


def test_local_public_payload_never_authorizes_a_formal_submission(problem):
    problem["reference"] = "private reference"
    payload = project_public_problem(problem)
    assert set(payload) == {"evaluator", "language", "function", "model_kind", "tests"}
    assert all(case["public"] is True for case in payload["tests"])
    assert "secret-scenario" not in json.dumps(payload) and "private reference" not in json.dumps(
        payload
    )
    validate_public_problem(payload)
    sandbox = RecordingSandbox(execution({"weights": [2], "bias": 3}))
    assert evaluate_public(payload, "source", sandbox)["total"] == 1
    with pytest.raises(MLContractError):
        python_ml_linear_v1(payload, "source", "submit", sandbox)
    payload["allow_public_only"] = True
    with pytest.raises(MLContractError):
        validate_public_problem(payload)


def test_local_public_payload_rejects_hidden_cases_and_does_not_alias_source(problem):
    payload = project_public_problem(problem)
    payload["tests"][0]["train"]["targets"][0] = 100
    assert problem["tests"][0]["train"]["targets"][0] != 100
    with pytest.raises(MLContractError):
        validate_public_problem(payload)
    with pytest.raises(MLContractError):
        validate_public_problem(problem)


def test_hidden_failure_has_no_private_metrics_or_model(problem):
    sandbox = RecordingSandbox(
        execution({"weights": [2], "bias": 3}), execution({"weights": [0], "bias": 987654})
    )
    result = python_ml_linear_v1(problem, "source", "submit", sandbox)
    assert (result["verdict"], result["passed"], result["total"]) == ("wrong_answer", 1, 2)
    assert len(result["public_cases"]) == 1
    assert "987654" not in json.dumps(result) and "secret-scenario" not in json.dumps(result)


def test_both_error_thresholds_are_enforced(problem):
    problem["tests"][0]["thresholds"] = {"mse": 2, "mae": 0.5}
    sandbox = RecordingSandbox(execution({"weights": [2], "bias": 4}))
    result = python_ml_linear_v1(problem, "source", "submit", sandbox)
    assert result["verdict"] == "wrong_answer"
    assert result["public_cases"][0]["metrics"] == {"mse": 1, "mae": 1}
    assert len(sandbox.requests) == 1


@pytest.mark.parametrize(
    "model",
    [
        None,
        [],
        3,
        True,
        "model",
        {},
        {"weights": [2]},
        {"weights": [2], "bias": 3, "mse": 0, "seed": 17},
        {"weights": [2, 2], "bias": 3},
        {"weights": (2,), "bias": 3},
        {"weights": [True], "bias": 3},
        {"weights": [2], "bias": False},
        {"weights": [float("nan")], "bias": 3},
        {"weights": [float("inf")], "bias": 3},
        {"weights": [2], "bias": float("-inf")},
        {"weights": [10**1000], "bias": 3},
        {"weights": [1_000_001], "bias": 3},
        {"weights": [2], "bias": 1_000_001},
    ],
)
def test_untrusted_model_rejection_is_wrong_answer_without_echoing_values(problem, model):
    result = python_ml_linear_v1(problem, "source", "submit", RecordingSandbox(execution(model)))
    assert result["verdict"] == "wrong_answer" and result["passed"] == 0
    assert set(result["public_cases"][0]) == {"name", "verdict", "label"}


@pytest.mark.parametrize(
    "verdict", ["syntax_error", "runtime_error", "time_limit", "memory_limit", "output_limit"]
)
def test_actual_sandbox_failure_classifications_are_preserved(problem, verdict):
    result = python_ml_linear_v1(
        problem, "source", "submit", RecordingSandbox(execution(verdict=verdict))
    )
    assert result["verdict"] == verdict


@pytest.mark.parametrize(
    "result",
    [
        execution(verdict="internal_error"),
        execution(verdict="unknown"),
        execution(elapsed=True),
        execution(elapsed=-1),
    ],
)
def test_platform_errors_are_not_learner_wrong_answers(problem, result):
    with pytest.raises(MLContractError):
        python_ml_linear_v1(problem, "source", "submit", RecordingSandbox(result))


def test_cancellation_and_sandbox_unavailability_propagate(problem):
    sandbox = RecordingSandbox()

    def cancelled():
        raise SandboxUnavailable("Lease lost")

    with pytest.raises(SandboxUnavailable, match="Lease lost"):
        python_ml_linear_v1(problem, "source", "submit", sandbox, on_case=cancelled)
    assert sandbox.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluator", "python-function-v1"),
        ("language", "javascript"),
        ("model_kind", "arbitrary-code"),
        ("function", "train();attack()"),
        ("tests", []),
        ("tests", "cases"),
    ],
)
def test_top_level_contract_rejects_invalid_configuration(problem, field, value):
    problem[field] = value
    with pytest.raises(MLContractError):
        validate_problem(problem)


@pytest.mark.parametrize(
    "path,value",
    [
        (("public",), 1),
        (("name",), ""),
        (("name",), "\x00"),
        (("train", "features"), [[1], [2], [3]]),
        (("train", "features"), [[1], [2, 3], [4], [5]]),
        (("train", "features"), [[1], [1], [3], [4]]),
        (("train", "features"), [[1] * 9] * 4),
        (("train", "targets"), [1, 2]),
        (("train", "targets"), [1, 2, 3, True]),
        (("train", "targets"), [1, 2, 3, float("inf")]),
        (("train", "targets"), [1, 2, 3, 1_000_001]),
        (("holdout", "features"), [[1, 2], [3, 4], [5, 6], [7, 8]]),
        (("holdout", "features"), [[False], [5], [6], [7]]),
        (("thresholds", "mse"), True),
        (("thresholds", "mse"), -1),
        (("thresholds", "mae"), 1_000_000_000_001),
        (("dataset", "revision"), True),
        (("dataset", "revision"), 0),
        (("dataset", "seed"), True),
        (("dataset", "seed"), -1),
        (("dataset", "seed"), 2**32),
        (("dataset", "id"), "../unsafe"),
        (("dataset", "digest"), "F" * 64),
    ],
)
def test_malformed_hidden_configuration_fails_before_any_execution(problem, path, value):
    current = problem["tests"][1]
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    sandbox = RecordingSandbox()
    with pytest.raises(MLContractError, match="^Invalid versioned ML evaluation contract.$"):
        python_ml_linear_v1(problem, "source", "run", sandbox)
    assert sandbox.requests == []


def test_hash_integrity_no_train_holdout_reuse_and_unknown_fields(problem):
    case = problem["tests"][1]
    case["train"]["targets"][0] += 0.01
    with pytest.raises(MLContractError):
        validate_problem(problem)
    rehash(problem)
    validate_problem(problem)
    case["holdout"]["features"][0] = list(case["train"]["features"][0])
    rehash(problem)
    with pytest.raises(MLContractError):
        validate_problem(problem)
    case["holdout"]["features"][0] = [55]
    case["train"]["reported_score"] = 0
    rehash(problem)
    with pytest.raises(MLContractError):
        validate_problem(problem)


def test_dataset_seed_is_versioned_provenance_not_learner_scoring(problem):
    problem["tests"][0]["dataset"]["seed"] = 999
    with pytest.raises(MLContractError):
        validate_problem(problem)
    rehash(problem)
    result = python_ml_linear_v1(
        problem, "source", "run", RecordingSandbox(execution({"weights": [2], "bias": 3}))
    )
    assert result["verdict"] == "accepted"


def test_visibility_case_and_dataset_identity_limits(problem):
    invalids = []
    for key, value in [("public", True), ("name", "public")]:
        item = copy.deepcopy(problem)
        item["tests"][1][key] = value
        invalids.append(item)
    item = copy.deepcopy(problem)
    item["tests"][1]["dataset"]["id"] = "public"
    rehash(item)
    invalids.append(item)
    item = copy.deepcopy(problem)
    item["tests"] *= 5
    invalids.append(item)
    item = copy.deepcopy(problem)
    item["reference"] = "x" * 1_000_001
    invalids.append(item)
    for invalid in invalids:
        with pytest.raises(MLContractError):
            validate_problem(invalid)


def test_strict_json_content_load_rejects_duplicates_overflow_and_oversize(problem, tmp_path):
    path = tmp_path / "problem.json"
    path.write_text(json.dumps(problem))
    assert load_problem(path) == problem
    for raw in (
        json.dumps(problem).replace(
            '"language": "python"', '"language":"javascript","language":"python"'
        ),
        json.dumps(problem).replace('"mse": 0', '"mse":NaN'),
        json.dumps(problem).replace('"mse": 0', '"mse":1e999'),
        "x" * 1_000_001,
    ):
        path.write_text(raw)
        with pytest.raises(MLContractError):
            load_problem(path)
