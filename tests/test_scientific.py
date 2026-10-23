"""Independent rational oracle and protocol tests; doubles do not prove execution."""

import copy
import json
import math
from fractions import Fraction

import pytest

from judge.sandbox import Execution, SandboxUnavailable
from judge.scientific import (
    ScientificContractError,
    assess_fit,
    dataset_digest,
    evaluate_public,
    fit_oracle,
    load_problem,
    load_public_problem,
    project_public_problem,
    python_weighted_fit_v1,
    validate_problem,
    validate_public_problem,
)


@pytest.fixture
def observations():
    return {"x": [-1, 0, 1, 2], "y": [1, 0, 1, 2], "sigma": [1, 1, 1, 1]}


@pytest.fixture
def problem(observations):
    cases = []
    for public, name in [(True, "public"), (False, "private-measurements")]:
        case = {
            "name": name,
            "public": public,
            "observations": copy.deepcopy(observations),
            "dataset": {"id": name, "revision": 1, "seed": 19, "digest": ""},
        }
        case["dataset"]["digest"] = dataset_digest(case)
        cases.append(case)
    return {
        "evaluator": "python-weighted-fit-v1",
        "language": "python",
        "function": "fit",
        "uncertainty_model": "known-independent-sigma-v1",
        "tests": cases,
    }


def model(oracle):
    return {key: copy.deepcopy(oracle[key]) for key in ("parameters", "covariance")}


class RecordingSandbox:
    def __init__(self, *executions):
        self.executions = iter(executions)
        self.requests = []

    def execute(self, source, function, args):
        self.requests.append((source, function, copy.deepcopy(args)))
        return next(self.executions)


def exact_rational_oracle(data):
    # Exact rational raw normal equations are independent of production's
    # floating-point centered implementation, including the offset cases.
    x, y, sigma = ([Fraction(str(value)) for value in data[key]] for key in ("x", "y", "sigma"))
    weights = [1 / s**2 for s in sigma]
    sw = sum(weights)
    sx = sum(w * xx for w, xx in zip(weights, x, strict=True))
    sy = sum(w * yy for w, yy in zip(weights, y, strict=True))
    sxx = sum(w * xx**2 for w, xx in zip(weights, x, strict=True))
    sxy = sum(w * xx * yy for w, xx, yy in zip(weights, x, y, strict=True))
    determinant = sw * sxx - sx**2
    return {
        "parameters": [
            float((sxx * sy - sx * sxy) / determinant),
            float((sw * sxy - sx * sy) / determinant),
        ],
        "covariance": [
            [float(sxx / determinant), float(-sx / determinant)],
            [float(-sx / determinant), float(sw / determinant)],
        ],
    }


@pytest.mark.parametrize(
    "offset,scale,sigma",
    [
        (0, 1, [0.2, 0.5, 0.3, 0.8]),
        (5000, 10, [0.2, 0.5, 0.3, 0.8]),
        (1, 0.002, [0.2, 0.5, 0.3, 0.8]),
        (0, 1, [0.01, 1, 0.02, 0.7]),
    ],
)
def test_centered_oracle_matches_independent_exact_rational_normal_equations(offset, scale, sigma):
    data = {
        "x": [offset + value * scale for value in [-3, -1, 1, 4]],
        "y": [-2, 0.3, 1.2, 4],
        "sigma": sigma,
    }
    actual, expected = fit_oracle(data), exact_rational_oracle(data)
    assert actual["parameters"] == pytest.approx(expected["parameters"], rel=1e-10, abs=1e-10)
    for actual_row, expected_row in zip(actual["covariance"], expected["covariance"], strict=True):
        assert actual_row == pytest.approx(expected_row, rel=1e-10, abs=1e-10)
    assert assess_fit(expected, data)["accepted"]


def test_manual_fit_covariance_chi2_and_degrees_of_freedom(observations):
    expected = {"parameters": [0.8, 0.4], "covariance": [[0.3, -0.1], [-0.1, 0.2]]}
    result = assess_fit(expected, observations)
    assert result["accepted"] and all(result["checks"].values())
    assert result["metrics"]["chi2"] == pytest.approx(1.2)
    assert result["metrics"]["ndof"] == 2
    assert result["metrics"]["reduced_chi2"] == pytest.approx(0.6)
    assert result["metrics"]["weighted_normal_residual"] < 1e-14
    assert result["model_diagnostic"] == "within_declared_scale"


def test_sigma_scaling_changes_covariance_and_chi2_without_changing_parameters(observations):
    base = fit_oracle(observations)
    scaled = {**observations, "sigma": [3] * 4}
    other = fit_oracle(scaled)
    assert other["parameters"] == pytest.approx(base["parameters"])
    for i in range(2):
        for j in range(2):
            assert other["covariance"][i][j] == pytest.approx(9 * base["covariance"][i][j])
    result = assess_fit(model(other), scaled)
    assert result["accepted"]
    assert result["metrics"]["chi2"] == pytest.approx(1.2 / 9)
    assert result["model_diagnostic"] == "below_declared_scale"
    assert not assess_fit(model(base), scaled)["checks"]["covariance"]


def test_x_unit_change_transforms_covariance_and_slope(observations):
    base = fit_oracle(observations)
    scaled = {**observations, "x": [100 * value for value in observations["x"]]}
    other = fit_oracle(scaled)
    assert other["parameters"] == pytest.approx(
        [base["parameters"][0], base["parameters"][1] / 100]
    )
    assert other["covariance"][0][0] == pytest.approx(base["covariance"][0][0])
    assert other["covariance"][0][1] == pytest.approx(base["covariance"][0][1] / 100)
    assert other["covariance"][1][1] == pytest.approx(base["covariance"][1][1] / 10_000)
    assert assess_fit(model(other), scaled)["metrics"]["chi2"] == pytest.approx(1.2)


def test_high_chi2_correct_fit_is_accepted_with_separate_model_diagnostic(observations):
    observations["sigma"] = [0.01] * 4
    result = assess_fit(model(fit_oracle(observations)), observations)
    assert result["accepted"] and result["metrics"]["reduced_chi2"] > 100
    assert result["model_diagnostic"] == "above_declared_scale"
    invalid = {"parameters": [900, 300], "covariance": [[1, 0], [0, 1]]}
    assert assess_fit(invalid, observations)["model_diagnostic"] == "not_assessed"


@pytest.mark.parametrize(
    "covariance",
    [
        [[-1, 0], [0, 1]],
        [[0, 0], [0, 0]],
        [[1, 2], [2, 1]],
        [[0.3, 0], [0, 0.2]],
        [[0.3, -0.1], [-0.2, 0.2]],
        [[1e-20, 0], [0, 1e-20]],
        [[0.18, -0.06], [-0.06, 0.12]],
    ],
)
def test_uncertainty_checks_reject_indefinite_tiny_rescaled_or_missing_correlation(
    observations, covariance
):
    result = assess_fit({"parameters": [0.8, 0.4], "covariance": covariance}, observations)
    assert not result["accepted"] and result["checks"]["parameters"]
    assert not result["checks"]["covariance"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        True,
        {"parameters": [0.8, 0.4]},
        {"parameters": [0.8, 0.4], "covariance": [[1, 0], [0, 1]], "chi2": 0},
        {"parameters": [True, 0.4], "covariance": [[1, 0], [0, 1]]},
        {"parameters": [0.8, 0.4], "covariance": [[1, False], [0, 1]]},
        {"parameters": [0.8, 0.4], "covariance": [[float("nan"), 0], [0, 1]]},
        {"parameters": [0.8, 0.4], "covariance": [[1e13, 0], [0, 1]]},
        {"parameters": [10**1000, 0.4], "covariance": [[1, 0], [0, 1]]},
        {"parameters": [float("inf"), 0.4], "covariance": [[1, 0], [0, 1]]},
        {"parameters": [0.8, 0.4], "covariance": [1, 2]},
    ],
)
def test_malformed_learner_model_is_rejected_without_echo(observations, value):
    assert assess_fit(value, observations) is None


def test_public_host_feedback_and_hidden_model_privacy(problem, observations):
    correct = model(fit_oracle(observations))
    sandbox = RecordingSandbox(Execution("ok", correct, 3), Execution("ok", correct, 5))
    result = python_weighted_fit_v1(problem, "source", "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == result["total"] == 2
    assert result["elapsed_ms"] == 8
    assert (
        sandbox.requests
        == [("source", "fit", [observations[key] for key in ("x", "y", "sigma")])] * 2
    )
    public = result["public_cases"]
    assert len(public) == 1 and set(public[0]) == {
        "name",
        "verdict",
        "label",
        "checks",
        "metrics",
        "model_diagnostic",
    }
    assert public[0]["metrics"]["chi2"] == pytest.approx(1.2)
    assert set(public[0]["checks"]) == {"parameters", "covariance", "stationarity"}
    assert all(type(value) is bool for value in public[0]["checks"].values())
    encoded = json.dumps(result)
    for forbidden in ("private-measurements", "observations", "seed", "digest"):
        assert forbidden not in encoded
    sandbox = RecordingSandbox(
        Execution("ok", correct),
        Execution("ok", {"parameters": [987654, 0], "covariance": [[1, 0], [0, 1]]}),
    )
    result = python_weighted_fit_v1(problem, "source", "submit", sandbox)
    assert result["verdict"] == "wrong_answer" and result["passed"] == 1
    assert "987654" not in json.dumps(result)


def test_run_and_lease_cancellation(problem, observations):
    sandbox = RecordingSandbox(Execution("ok", model(fit_oracle(observations))))
    result = python_weighted_fit_v1(problem, "source", "run", sandbox)
    assert result["total"] == result["passed"] == 1 and len(sandbox.requests) == 1

    def cancel():
        raise SandboxUnavailable("Lease lost")

    with pytest.raises(SandboxUnavailable, match="Lease lost"):
        python_weighted_fit_v1(problem, "source", "submit", sandbox, on_case=cancel)
    assert len(sandbox.requests) == 1


@pytest.mark.parametrize(
    "verdict", ["syntax_error", "runtime_error", "time_limit", "memory_limit", "output_limit"]
)
def test_sandbox_errors_preserved_without_learner_message(problem, verdict):
    result = python_weighted_fit_v1(
        problem, "source", "submit", RecordingSandbox(Execution(verdict, "private message", 3))
    )
    assert result["verdict"] == verdict
    assert "private message" not in json.dumps(result)


@pytest.mark.parametrize(
    "execution",
    [
        Execution("internal_error"),
        Execution("unknown"),
        Execution("ok", {}, -1),
        Execution("ok", {}, True),
    ],
)
def test_platform_failure_not_counted_as_learner_error(problem, execution):
    with pytest.raises(ScientificContractError):
        python_weighted_fit_v1(problem, "source", "submit", RecordingSandbox(execution))


@pytest.mark.parametrize(
    "key,value",
    [
        ("x", [1, 1, 1, 1]),
        ("x", [1, 1.0000001, 1.0000002, 1.0000003]),
        ("x", [9999, 9999.1, 9999.2, 9999.3]),
        ("x", [1, 2, 3]),
        ("x", [1, 2, 3, True]),
        ("x", [1, 2, 3, 10001]),
        ("y", [1, 2, 3, float("nan")]),
        ("y", [1, 2, 3, 10001]),
        ("sigma", [1, 1, 1, 0]),
        ("sigma", [1, 1, 1, -1]),
        ("sigma", [1, 1, 1, True]),
        ("sigma", [1, 1, 1, 0.001]),
        ("sigma", [1, 1, 1, 101]),
        ("sigma", [0.01, 1.01, 1, 1]),
        ("sigma", [1, 1, 1, float("inf")]),
    ],
)
def test_bad_or_ill_conditioned_hidden_content_rejected_before_execution(problem, key, value):
    problem["tests"][1]["observations"][key] = value
    sandbox = RecordingSandbox()
    with pytest.raises(
        ScientificContractError, match="^Invalid versioned scientific evaluation contract.$"
    ):
        python_weighted_fit_v1(problem, "source", "run", sandbox)
    assert sandbox.requests == []


def test_strict_content_identity_visibility_and_hash(problem, tmp_path):
    validate_problem(problem)
    path = tmp_path / "problem.json"
    path.write_text(json.dumps(problem))
    assert load_problem(path) == problem
    for raw in (
        json.dumps(problem).replace(
            '"language": "python"', '"language":"javascript","language":"python"'
        ),
        json.dumps(problem).replace('"sigma": [1, 1, 1, 1]', '"sigma":[1,1,1,1e999]'),
        "x" * 1_000_001,
    ):
        path.write_text(raw)
        with pytest.raises(ScientificContractError):
            load_problem(path)
    case = problem["tests"][1]
    case["dataset"]["seed"] += 1
    with pytest.raises(ScientificContractError):
        validate_problem(problem)
    case["dataset"]["digest"] = dataset_digest(case)
    validate_problem(problem)
    case["public"] = True
    with pytest.raises(ScientificContractError):
        validate_problem(problem)


def test_parameter_and_covariance_tolerances_are_not_replaced_by_small_chi2(observations):
    value = model(fit_oracle(observations))
    value["parameters"][0] += 1e-4
    result = assess_fit(value, observations)
    assert not result["accepted"] and not result["checks"]["parameters"]
    assert math.isclose(result["metrics"]["chi2"], 1.2, rel_tol=1e-6)


def test_oracle_scaled_covariance_tolerance_rejects_tiny_wrong_slope_variance():
    observations = {
        "x": [-10000, -5000, 5000, 10000],
        "y": [-997, -497, 503, 1003],
        "sigma": [0.01] * 4,
    }
    value = model(fit_oracle(observations))
    assert 0 < value["covariance"][1][1] < 1e-10
    value["covariance"][1][1] = 1e-20
    result = assess_fit(value, observations)
    assert result["checks"]["parameters"]
    assert not result["checks"]["covariance"] and not result["accepted"]


def test_stationarity_prevents_large_intercept_relative_tolerance_from_hiding_bias():
    observations = {
        "x": [4980, 4990, 5010, 5020],
        "y": [-4000, -2000, 2000, 4000],
        "sigma": [0.01] * 4,
    }
    value = model(fit_oracle(observations))
    value["parameters"][0] += 0.01
    result = assess_fit(value, observations)
    assert result["checks"]["parameters"] and result["checks"]["covariance"]
    assert not result["checks"]["stationarity"] and not result["accepted"]


def test_public_projection_has_explicit_allowlist_and_does_not_modify_owner_data(
    problem, observations, tmp_path
):
    problem["reference"] = "private reference source"
    problem["owner_note"] = "not public"
    payload = project_public_problem(problem)
    assert set(payload) == {"evaluator", "language", "function", "uncertainty_model", "tests"}
    assert len(payload["tests"]) == 1
    assert "private" not in json.dumps(payload)
    validate_public_problem(payload)
    path = tmp_path / "public.json"
    path.write_text(json.dumps(payload))
    assert load_public_problem(path) == payload
    with pytest.raises(ScientificContractError):
        validate_problem(payload)
    sandbox = RecordingSandbox(Execution("ok", model(fit_oracle(observations))))
    result = evaluate_public(payload, "source", sandbox)
    assert result["passed"] == result["total"] == 1
    payload["tests"][0]["observations"]["x"][0] = 99
    assert problem["tests"][0]["observations"]["x"][0] == -1


@pytest.mark.parametrize(
    "mutation", ["private-case", "extra-reference", "content-switch", "no-cases"]
)
def test_public_evaluator_rejects_private_data_and_cannot_be_promoted_to_formal(problem, mutation):
    payload = project_public_problem(problem)
    if mutation == "private-case":
        payload["tests"].append(copy.deepcopy(problem["tests"][1]))
    elif mutation == "extra-reference":
        payload["reference"] = "private source"
    elif mutation == "content-switch":
        payload["allow_public_only"] = True
    else:
        payload["tests"] = []
    sandbox = RecordingSandbox()
    with pytest.raises(ScientificContractError):
        evaluate_public(payload, "source", sandbox)
    assert sandbox.requests == []
