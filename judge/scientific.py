"""Known-error weighted line fitting with independent host uncertainty checks."""

import hashlib
import json
import math
import re
from pathlib import Path

from judge.results import LABELS

EVALUATOR = "python-weighted-fit-v1"
UNCERTAINTY_MODEL = "known-independent-sigma-v1"
METRIC_VERSION = "weighted-line-diagnostics-v1"
MAX_BYTES = 1_000_000
PARAMETER_ATOL = 1e-8
PARAMETER_RTOL = 1e-7
COVARIANCE_SCALED_ATOL = 1e-8
COVARIANCE_RTOL = 1e-7
STATIONARITY_ATOL = 1e-5
FAILURES = frozenset(LABELS) - {"accepted", "internal_error"}


class ScientificContractError(ValueError):
    """Invalid trusted configuration, not a learner Wrong Answer."""


def _invalid():
    return ScientificContractError("Invalid versioned scientific evaluation contract.")


def _number(value, maximum):
    return type(value) in {int, float} and abs(value) <= maximum and math.isfinite(value)


def _canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None


def dataset_digest(case):
    try:
        value = {
            "dataset": {key: value for key, value in case["dataset"].items() if key != "digest"},
            "observations": case["observations"],
        }
    except (KeyError, TypeError, AttributeError):
        raise _invalid() from None
    return hashlib.sha256(_canonical(value)).hexdigest()


def fit_oracle(observations):
    """Validate bounded data and compute a centered analytic weighted fit.

    Sigma is an absolute known measurement uncertainty. The inverse information
    covariance is not rescaled by the observed residual variance.
    """
    if type(observations) is not dict or set(observations) != {"x", "y", "sigma"}:
        raise _invalid()
    x, y, sigma = (observations[key] for key in ("x", "y", "sigma"))
    if (
        type(x) is not list
        or not 4 <= len(x) <= 64
        or type(y) is not list
        or len(y) != len(x)
        or type(sigma) is not list
        or len(sigma) != len(x)
        or any(not _number(value, 10_000) for value in x + y)
        or any(not _number(value, 100) or value < 0.01 for value in sigma)
        or max(sigma) / min(sigma) > 100
    ):
        raise _invalid()
    weights = [1 / value**2 for value in sigma]
    sw = math.fsum(weights)
    mean_x = math.fsum(w * value for w, value in zip(weights, x, strict=True)) / sw
    mean_y = math.fsum(w * value for w, value in zip(weights, y, strict=True)) / sw
    centered = [value - mean_x for value in x]
    sxx = math.fsum(w * dx * dx for w, dx in zip(weights, centered, strict=True))
    variance_x = sxx / sw
    # Offset/spread ratio is invariant under x-unit rescaling. The additional
    # absolute spread floor prevents near-rank-deficiency and unstable extrapolation
    # of the intercept back to x=0. Repeated x values are legitimate replicates.
    if variance_x < 1e-6 or abs(mean_x) > 1000 * math.sqrt(variance_x):
        raise _invalid()
    sxy = math.fsum(
        w * dx * (value - mean_y) for w, dx, value in zip(weights, centered, y, strict=True)
    )
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    covariance = [[1 / sw + mean_x**2 / sxx, -mean_x / sxx], [-mean_x / sxx, 1 / sxx]]
    if any(not _number(value, 1e8) for value in (intercept, slope)) or any(
        not _number(value, 1e12) for row in covariance for value in row
    ):
        raise _invalid()
    return {
        "parameters": [intercept, slope],
        "covariance": covariance,
        "weights": weights,
        "mean_x": mean_x,
        "sum_weights": sw,
        "centered_sum": sxx,
    }


def _validate(problem, *, public_only):
    if (
        type(problem) is not dict
        or problem.get("evaluator") != EVALUATOR
        or problem.get("language") != "python"
        or problem.get("uncertainty_model") != UNCERTAINTY_MODEL
        or type(problem.get("function")) is not str
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", problem["function"])
        or type(problem.get("tests")) is not list
        or not (1 if public_only else 2) <= len(problem["tests"]) <= 8
    ):
        raise _invalid()
    names, datasets, visibility = set(), set(), set()
    for case in problem["tests"]:
        if type(case) is not dict or set(case) != {"name", "public", "observations", "dataset"}:
            raise _invalid()
        if (
            type(case["name"]) is not str
            or not 1 <= len(case["name"]) <= 120
            or not case["name"].strip()
            or "\0" in case["name"]
            or case["name"] in names
            or type(case["public"]) is not bool
        ):
            raise _invalid()
        names.add(case["name"])
        visibility.add(case["public"])
        fit_oracle(case["observations"])
        data = case["dataset"]
        if (
            type(data) is not dict
            or set(data) != {"id", "revision", "seed", "digest"}
            or type(data["id"]) is not str
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", data["id"])
            or type(data["revision"]) is not int
            or not 1 <= data["revision"] <= 2**31 - 1
            or type(data["seed"]) is not int
            or not 0 <= data["seed"] <= 2**32 - 1
            or type(data["digest"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", data["digest"])
            or (data["id"], data["revision"]) in datasets
            or data["digest"] != dataset_digest(case)
        ):
            raise _invalid()
        datasets.add((data["id"], data["revision"]))
    if (
        visibility != ({True} if public_only else {True, False})
        or len(_canonical(problem)) > MAX_BYTES
    ):
        raise _invalid()


def validate_problem(problem):
    """Complete owner content for service evaluation always requires hidden tests."""
    _validate(problem, public_only=False)


PUBLIC_FIELDS = {"evaluator", "language", "function", "uncertainty_model", "tests"}


def validate_public_problem(problem):
    """A separate minimal public execution payload, never a formal submission."""
    if type(problem) is not dict or set(problem) != PUBLIC_FIELDS:
        raise _invalid()
    _validate(problem, public_only=True)


def project_public_problem(version):
    validate_problem(version)
    result = {key: version[key] for key in PUBLIC_FIELDS - {"tests"}}
    result["tests"] = [
        {
            "name": case["name"],
            "public": True,
            "observations": {key: list(case["observations"][key]) for key in ("x", "y", "sigma")},
            "dataset": {key: case["dataset"][key] for key in ("id", "revision", "seed", "digest")},
        }
        for case in version["tests"]
        if case["public"]
    ]
    return result


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(value):
    raise _invalid()


def _load(path, validator):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _invalid()
    try:
        result = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    validator(result)
    return result


def load_problem(path):
    return _load(path, validate_problem)


def load_public_problem(path):
    return _load(path, validate_public_problem)


def assess_fit(model, observations, oracle=None):
    """Return host diagnostics, never the raw learner model or oracle parameters."""
    oracle = oracle if oracle is not None else fit_oracle(observations)
    if type(model) is not dict or set(model) != {"parameters", "covariance"}:
        return None
    parameters, covariance = model["parameters"], model["covariance"]
    if (
        type(parameters) is not list
        or len(parameters) != 2
        or any(not _number(v, 1e8) for v in parameters)
        or type(covariance) is not list
        or len(covariance) != 2
        or any(
            type(row) is not list or len(row) != 2 or any(not _number(v, 1e12) for v in row)
            for row in covariance
        )
    ):
        return None
    expected_covariance = oracle["covariance"]
    parameters_ok = all(
        math.isclose(value, expected, rel_tol=PARAMETER_RTOL, abs_tol=PARAMETER_ATOL)
        for value, expected in zip(parameters, oracle["parameters"], strict=True)
    )
    covariance_ok = True
    normalized_error = 0.0
    for i in range(2):
        for j in range(2):
            scale = math.sqrt(expected_covariance[i][i]) * math.sqrt(expected_covariance[j][j])
            difference = abs(covariance[i][j] - expected_covariance[i][j])
            normalized_error = max(normalized_error, difference / scale)
            covariance_ok &= difference <= COVARIANCE_SCALED_ATOL * scale + COVARIANCE_RTOL * abs(
                expected_covariance[i][j]
            )
    # Allow roundoff-level asymmetry only; PD is checked on the symmetric part.
    symmetric = abs(covariance[0][1] - covariance[1][0]) <= COVARIANCE_SCALED_ATOL * math.sqrt(
        expected_covariance[0][0] * expected_covariance[1][1]
    )
    off_diagonal = (covariance[0][1] + covariance[1][0]) / 2
    positive = (
        covariance[0][0] > 0
        and covariance[1][1] > 0
        and abs(off_diagonal) < math.sqrt(covariance[0][0]) * math.sqrt(covariance[1][1])
    )
    covariance_ok = bool(covariance_ok and symmetric and positive)
    x, y, sigma = (observations[key] for key in ("x", "y", "sigma"))
    residuals = [parameters[0] + parameters[1] * xx - yy for xx, yy in zip(x, y, strict=True)]
    chi2 = math.fsum(
        (residual / error) ** 2 for residual, error in zip(residuals, sigma, strict=True)
    )
    grad0 = math.fsum(w * r for w, r in zip(oracle["weights"], residuals, strict=True))
    grad1 = math.fsum(
        w * (xx - oracle["mean_x"]) * r
        for w, xx, r in zip(oracle["weights"], x, residuals, strict=True)
    )
    stationarity = max(
        abs(grad0) / math.sqrt(oracle["sum_weights"]),
        abs(grad1) / math.sqrt(oracle["centered_sum"]),
    )
    ndof = len(x) - 2
    reduced = chi2 / ndof
    checks = {
        "parameters": parameters_ok,
        "covariance": covariance_ok,
        "stationarity": stationarity <= STATIONARITY_ATOL,
    }
    accepted = all(checks.values())
    diagnostic = "not_assessed"
    if accepted:
        diagnostic = (
            "above_declared_scale"
            if reduced > 3
            else "below_declared_scale"
            if reduced < 0.25
            else "within_declared_scale"
        )
    return {
        "accepted": accepted,
        "checks": checks,
        "metrics": {
            "chi2": chi2,
            "ndof": ndof,
            "reduced_chi2": reduced,
            "weighted_normal_residual": stationarity,
            "covariance_normalized_error": normalized_error,
        },
        "model_diagnostic": diagnostic,
    }


def python_weighted_fit_v1(version, source, mode, sandbox, on_case=lambda: None):
    validate_problem(version)
    return _evaluate_validated(version, source, mode, sandbox, on_case)


def evaluate_public(version, source, sandbox, on_case=lambda: None):
    validate_public_problem(version)
    return _evaluate_validated(version, source, "run", sandbox, on_case)


def _evaluate_validated(version, source, mode, sandbox, on_case):
    if mode not in {"run", "submit"} or type(source) is not str or len(source) > 20_000:
        raise _invalid()
    cases = [case for case in version["tests"] if mode == "submit" or case["public"]]
    feedback, passed, elapsed, verdict = [], 0, 0, "accepted"
    for case in cases:
        on_case()
        observations = case["observations"]
        execution = sandbox.execute(
            source, version["function"], [observations[key] for key in ("x", "y", "sigma")]
        )
        if type(execution.elapsed_ms) is not int or execution.elapsed_ms < 0:
            raise ScientificContractError("Invalid sandbox result metadata.")
        elapsed += execution.elapsed_ms
        outcome, assessment = execution.verdict, None
        if outcome == "ok":
            assessment = assess_fit(execution.value, observations)
            outcome = (
                "accepted" if assessment is not None and assessment["accepted"] else "wrong_answer"
            )
        elif outcome not in FAILURES:
            raise ScientificContractError("Unavailable or invalid sandbox execution result.")
        if case["public"]:
            public = {"name": case["name"], "verdict": outcome, "label": LABELS[outcome]}
            if assessment is not None:
                public.update(
                    {key: assessment[key] for key in ("checks", "metrics", "model_diagnostic")}
                )
            feedback.append(public)
        if outcome != "accepted":
            verdict = outcome
            break
        passed += 1
    return {
        "verdict": verdict,
        "label": LABELS[verdict],
        "passed": passed,
        "total": len(cases),
        "elapsed_ms": elapsed,
        "public_cases": feedback,
        "assessment": {
            "kind": UNCERTAINTY_MODEL,
            "metric_version": METRIC_VERSION,
            "visibility": "public-cases-only",
            "diagnostic_is_correctness": False,
        },
    }
