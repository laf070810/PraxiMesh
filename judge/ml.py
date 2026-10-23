"""Bounded supervised training: only the controller evaluates held-out predictions.

This module is a reviewed evaluator, never an executor. User source is handed to
the configured sandbox. Integration MUST include this file in policy_digest and
use the sandbox's strict duplicate-key/nonfinite JSON transport decoder.
"""

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from judge.results import LABELS

EVALUATOR = "python-ml-linear-v1"
MODEL_KIND = "linear-regression-v1"
METRIC_VERSION = "mse-mae-v1"
MAX_CASES = 8
MAX_ROWS = 128
MAX_DIMENSIONS = 8
MAX_ABS_NUMBER = 1_000_000
MAX_THRESHOLD = 1_000_000_000_000
MAX_PACKAGE_BYTES = 1_000_000
SOURCE_LIMIT = 20_000
HEX64 = re.compile(r"[a-f0-9]{64}")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
DATASET_ID = re.compile(r"[a-z][a-z0-9-]{0,79}")
FAILURES = frozenset(LABELS) - {"accepted", "internal_error"}


class MLContractError(ValueError):
    """Trusted content/configuration error; never a learner Wrong Answer."""


def _invalid():
    # Never include hidden rows, labels, names, model values, or thresholds.
    return MLContractError("Invalid versioned ML evaluation contract.")


def _number(value, maximum=MAX_ABS_NUMBER):
    return type(value) in {int, float} and abs(value) <= maximum and math.isfinite(value)


def _canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


def dataset_digest(case):
    """SHA256 of canonical {dataset-without-digest, train, holdout} UTF-8 JSON.

    The canonical JSON uses sorted keys, no spaces, ensure_ascii=False and
    allow_nan=False. Seed is provenance only; actual matrices/labels are hashed.
    Thresholds belong to the enclosing immutable problem version, not the data.
    """
    try:
        value = {
            "dataset": {key: value for key, value in case["dataset"].items() if key != "digest"},
            "train": case["train"],
            "holdout": case["holdout"],
        }
    except (KeyError, TypeError, AttributeError):
        raise _invalid() from None
    return hashlib.sha256(_canonical(value)).hexdigest()


def _split(split, dimension=None):
    if type(split) is not dict or set(split) != {"features", "targets"}:
        raise _invalid()
    features, targets = split["features"], split["targets"]
    if (
        type(features) is not list
        or not 4 <= len(features) <= MAX_ROWS
        or type(targets) is not list
        or len(targets) != len(features)
        or any(not _number(value) for value in targets)
    ):
        raise _invalid()
    if type(features[0]) is not list or not 1 <= len(features[0]) <= MAX_DIMENSIONS:
        raise _invalid()
    dimension = dimension or len(features[0])
    for row in features:
        if type(row) is not list or len(row) != dimension or any(not _number(v) for v in row):
            raise _invalid()
    rows = {tuple(row) for row in features}
    if len(rows) != len(features):
        raise _invalid()
    return dimension, rows


def _validate_problem(problem, *, public_only=False):
    """Supplement the common content schema; validate all scenarios before any execution."""
    if (
        type(problem) is not dict
        or problem.get("evaluator") != EVALUATOR
        or problem.get("language") != "python"
        or problem.get("model_kind") != MODEL_KIND
        or type(problem.get("function")) is not str
        or not IDENTIFIER.fullmatch(problem["function"])
        or type(problem.get("tests")) is not list
        or not (1 if public_only else 2) <= len(problem["tests"]) <= MAX_CASES
    ):
        raise _invalid()
    seen_names, seen_datasets, visibility = set(), set(), set()
    for case in problem["tests"]:
        if type(case) is not dict or set(case) != {
            "name",
            "public",
            "train",
            "holdout",
            "thresholds",
            "dataset",
        }:
            raise _invalid()
        if (
            type(case["name"]) is not str
            or not 1 <= len(case["name"]) <= 120
            or not case["name"].strip()
            or "\0" in case["name"]
            or case["name"] in seen_names
            or type(case["public"]) is not bool
        ):
            raise _invalid()
        seen_names.add(case["name"])
        visibility.add(case["public"])
        dimension, training_rows = _split(case["train"])
        _, heldout_rows = _split(case["holdout"], dimension)
        if training_rows & heldout_rows:
            raise _invalid()
        thresholds = case["thresholds"]
        if (
            type(thresholds) is not dict
            or set(thresholds) != {"mse", "mae"}
            or any(not _number(v, MAX_THRESHOLD) or v < 0 for v in thresholds.values())
        ):
            raise _invalid()
        identity = case["dataset"]
        if (
            type(identity) is not dict
            or set(identity) != {"id", "revision", "seed", "digest"}
            or type(identity["id"]) is not str
            or not DATASET_ID.fullmatch(identity["id"])
            or type(identity["revision"]) is not int
            or not 1 <= identity["revision"] <= 2**31 - 1
            or type(identity["seed"]) is not int
            or not 0 <= identity["seed"] <= 2**32 - 1
            or type(identity["digest"]) is not str
            or not HEX64.fullmatch(identity["digest"])
            or (identity["id"], identity["revision"]) in seen_datasets
            or identity["digest"] != dataset_digest(case)
        ):
            raise _invalid()
        seen_datasets.add((identity["id"], identity["revision"]))
    if (
        visibility != ({True} if public_only else {True, False})
        or len(_canonical(problem)) > MAX_PACKAGE_BYTES
    ):
        raise _invalid()


def validate_problem(problem):
    _validate_problem(problem)


PUBLIC_FIELDS = frozenset({"evaluator", "language", "function", "model_kind", "tests"})


def project_public_problem(problem):
    validate_problem(problem)
    result = {key: copy.deepcopy(problem[key]) for key in PUBLIC_FIELDS - {"tests"}}
    result["tests"] = [
        {
            key: copy.deepcopy(case[key])
            for key in ("name", "public", "train", "holdout", "thresholds", "dataset")
        }
        for case in problem["tests"]
        if case["public"]
    ]
    return result


def validate_public_problem(problem):
    if type(problem) is not dict or set(problem) != PUBLIC_FIELDS:
        raise _invalid()
    _validate_problem(problem, public_only=True)


def evaluate_public(problem, source, sandbox, on_case=lambda: None):
    validate_public_problem(problem)
    return _evaluate(problem, source, "run", sandbox, on_case)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(value):
    raise _invalid()


def load_problem(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise _invalid()
    try:
        problem = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    validate_problem(problem)
    return problem


def model_metrics(model, holdout):
    """Return trusted MSE/MAE or None for an invalid learner model. Never echo it."""
    dimension = len(holdout["features"][0])  # The complete content contract was validated first.
    if (
        type(model) is not dict
        or set(model) != {"weights", "bias"}
        or type(model["weights"]) is not list
        or len(model["weights"]) != dimension
        or any(not _number(v) for v in model["weights"])
        or not _number(model["bias"])
    ):
        return None
    errors = [
        math.fsum(weight * value for weight, value in zip(model["weights"], row, strict=True))
        + model["bias"]
        - target
        for row, target in zip(holdout["features"], holdout["targets"], strict=True)
    ]
    # Bounds above make products, sums and squared errors finite, even at the
    # allowed extremes; keep a final fail-closed check for future bound changes.
    mse = math.fsum(error * error for error in errors) / len(errors)
    mae = math.fsum(abs(error) for error in errors) / len(errors)
    if not math.isfinite(mse) or not math.isfinite(mae):
        return None
    return {"mse": mse, "mae": mae}


def python_ml_linear_v1(version, source, mode, sandbox, on_case=lambda: None):
    validate_problem(version)
    return _evaluate(version, source, mode, sandbox, on_case)


def _evaluate(version, source, mode, sandbox, on_case):
    if mode not in {"run", "submit"} or type(source) is not str or len(source) > SOURCE_LIMIT:
        raise _invalid()
    cases = [case for case in version["tests"] if mode == "submit" or case["public"]]
    passed, elapsed, feedback, verdict = 0, 0, [], "accepted"
    for case in cases:
        on_case()  # Lease cancellation propagates; it is never reported as Wrong Answer.
        execution = sandbox.execute(
            source, version["function"], [case["train"]["features"], case["train"]["targets"]]
        )
        if type(execution.elapsed_ms) is not int or execution.elapsed_ms < 0:
            raise MLContractError("Invalid sandbox result metadata.")
        elapsed += execution.elapsed_ms
        metrics = None
        outcome = execution.verdict
        if outcome == "ok":
            metrics = model_metrics(execution.value, case["holdout"])
            outcome = (
                "accepted"
                if metrics is not None
                and all(metrics[key] <= case["thresholds"][key] for key in ("mse", "mae"))
                else "wrong_answer"
            )
        elif outcome not in FAILURES:
            raise MLContractError("Unavailable or invalid sandbox execution result.")
        if case["public"]:
            public = {"name": case["name"], "verdict": outcome, "label": LABELS[outcome]}
            if metrics is not None:
                public["metrics"] = metrics
                public["thresholds"] = dict(case["thresholds"])
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
            "kind": MODEL_KIND,
            "metric_version": METRIC_VERSION,
            "visibility": "public-cases-only",
        },
    }
