"""Read-only checks for persisted sequential and specialist result contracts.

These checks reject incomplete/mismatched records. They do not authenticate
client-uploaded results; only the trusted worker may produce formal evidence.
"""

import copy
import hashlib
import math
import re

from judge.results import LABELS

HEX64 = re.compile(r"[a-f0-9]{64}")
VERDICTS = frozenset(LABELS) - {"internal_error"}
SEQUENTIAL_EVALUATORS = {
    "python-function-v1": "python",
    "javascript-function-v1": "javascript",
    "python-stdio-v1": "python",
}


class InvalidResult(ValueError):
    pass


def _verified(job, *, specialist=False):
    invalid = InvalidResult("Incomplete or incompatible formal result evidence")
    if (
        job.mode != "submit"
        or job.status != "finished"
        or job.verdict not in VERDICTS
        or job.started_at is None
        or job.finished_at is None
        or job.finished_at < job.started_at
        or not HEX64.fullmatch(job.evaluation_fingerprint)
        or not isinstance(job.result, dict)
        or not isinstance(job.environment, dict)
        or not isinstance(job.version.data, dict)
    ):
        raise invalid
    if getattr(job, "budget_state", "legacy") != "legacy":
        from core.resources import BUDGET_POLICY, reservation_units

        if (
            job.budget_state != "consumed"
            or job.budget_policy != BUDGET_POLICY
            or job.execution_started_at is None
            or not job.started_at <= job.execution_started_at <= job.finished_at
            or job.budget_units != reservation_units(job.version, "submit")
        ):
            raise invalid
    evaluator = job.version.data.get("evaluator")
    language = "python" if specialist else SEQUENTIAL_EVALUATORS.get(evaluator)
    if language is None or job.version.data.get("language") != language:
        raise invalid
    passed, total = job.result.get("passed"), job.result.get("total")
    cases = job.version.data.get("tests")
    if (
        type(passed) is not int
        or type(total) is not int
        or not isinstance(cases, list)
        or not 1 <= len(cases) <= 20
        or total != len(cases)
        or not 0 <= passed <= total
        or job.result.get("verdict") != job.verdict
        or (job.verdict == "accepted") != (passed == total)
    ):
        raise invalid
    environment = job.environment
    if (
        environment.get("language", language) != language
        or environment.get("evaluator", evaluator) != evaluator
        or (
            specialist
            and (
                environment.get("language") != language or environment.get("evaluator") != evaluator
            )
        )
    ):
        raise invalid
    policy_digest = environment.get("policy_digest")
    if not isinstance(policy_digest, str) or not HEX64.fullmatch(policy_digest):
        raise invalid
    policy = environment.get("policy")
    if policy == "wasi-functions-v2":
        if (
            environment.get("languages") != ["python", "javascript"]
            or environment.get("python") != "3.14.9"
            or environment.get("quickjs") != "2026-06-04"
            or environment.get("wasmtime") != "50.0.1"
        ):
            raise invalid
        backend, artifact = "wasi", environment.get("bundle")
    elif (
        not specialist and policy == "wasi-python-function-v1" and evaluator == "python-function-v1"
    ):
        backend, artifact = "wasi", environment.get("bundle")
    elif (
        not specialist
        and policy == "gvisor-python-function-v1"
        and evaluator == "python-function-v1"
    ):
        backend, artifact = "gvisor", environment.get("image")
    else:
        raise invalid
    if not isinstance(artifact, str) or not (
        HEX64.fullmatch(artifact)
        if backend == "wasi"
        else re.fullmatch(r"[^\s]{1,200}@sha256:[a-f0-9]{64}", artifact)
    ):
        raise invalid
    fingerprint = hashlib.sha256(f"{backend}:{artifact}:{policy_digest}".encode()).hexdigest()
    if fingerprint != job.evaluation_fingerprint:
        raise invalid
    return {"passed": passed, "total": total, "verdict": job.verdict}


SPECIALISTS = frozenset({"python-ml-linear-v1", "python-weighted-fit-v1", "python-agent-tools-v1"})


def verified_metrics(job):
    """Comparable sequential-test counts only; specialist results have no such board."""
    try:
        return _verified(job)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InvalidResult("Incomplete or incompatible formal result evidence") from None


def _number(value):
    return type(value) in {int, float} and 0 <= value <= 1e100 and math.isfinite(value)


def specialist_feedback(version, result, mode):
    """Validate the complete bounded feedback contract; never project unknown fields.

    Results are worker-owned evidence, not client-uploaded claims. Invalid payloads
    fail closed, including unknown nested fields that could contain hidden data.
    """
    invalid = InvalidResult("Invalid specialist feedback")
    evaluator = version.data.get("evaluator")
    if evaluator == "python-agent-tools-v1":
        from core.agent_evidence import public_feedback

        return public_feedback(version, result, mode)
    if evaluator not in SPECIALISTS or mode not in {"run", "submit"}:
        raise invalid
    cases = version.data.get("tests")
    if not isinstance(cases, list) or not 2 <= len(cases) <= 8:
        raise invalid
    selected = [case for case in cases if mode == "submit" or case.get("public") is True]
    if type(result) is not dict or set(result) != {
        "verdict",
        "label",
        "passed",
        "total",
        "elapsed_ms",
        "public_cases",
        "assessment",
    }:
        raise invalid
    passed, total, verdict = result["passed"], result["total"], result["verdict"]
    if (
        type(passed) is not int
        or type(total) is not int
        or total != len(selected)
        or not 0 <= passed <= total
        or verdict not in VERDICTS
        or (verdict == "accepted") != (passed == total)
        or result["label"] != LABELS[verdict]
        or type(result["elapsed_ms"]) is not int
        or result["elapsed_ms"] < 0
    ):
        raise invalid
    ml = evaluator == "python-ml-linear-v1"
    assessment = {
        "kind": "linear-regression-v1" if ml else "known-independent-sigma-v1",
        "metric_version": "mse-mae-v1" if ml else "weighted-line-diagnostics-v1",
        "visibility": "public-cases-only",
    }
    if not ml:
        assessment["diagnostic_is_correctness"] = False
    if (
        result["assessment"] != assessment
        or type(result["assessment"]) is not dict
        or any(
            type(result["assessment"][key]) is not type(value) for key, value in assessment.items()
        )
    ):
        raise invalid
    reached = selected[: passed + (verdict != "accepted")]
    public = [(index, case) for index, case in enumerate(reached) if case.get("public") is True]
    feedback = result["public_cases"]
    if type(feedback) is not list or len(feedback) != len(public):
        raise invalid
    for row, (index, case) in zip(feedback, public, strict=True):
        expected = "accepted" if index < passed else verdict
        base = {"name", "verdict", "label"}
        if (
            type(row) is not dict
            or row.get("name") != case.get("name")
            or row.get("verdict") != expected
            or row.get("label") != LABELS[expected]
        ):
            raise invalid
        if set(row) == base:
            if expected in {"accepted"}:
                raise invalid
            continue
        if expected not in {"accepted", "wrong_answer"}:
            raise invalid
        metrics = row.get("metrics")
        if ml:
            if (
                set(row) != base | {"metrics", "thresholds"}
                or type(metrics) is not dict
                or set(metrics) != {"mse", "mae"}
                or any(not _number(value) for value in metrics.values())
                or row["thresholds"] != case.get("thresholds")
                or any(not _number(value) for value in row["thresholds"].values())
                or (expected == "accepted")
                != all(metrics[key] <= row["thresholds"][key] for key in metrics)
            ):
                raise invalid
        else:
            keys = {
                "chi2",
                "ndof",
                "reduced_chi2",
                "weighted_normal_residual",
                "covariance_normalized_error",
            }
            checks = row.get("checks")
            if (
                set(row) != base | {"checks", "metrics", "model_diagnostic"}
                or type(metrics) is not dict
                or set(metrics) != keys
                or any(not _number(value) for value in metrics.values())
                or type(metrics["ndof"]) is not int
                or metrics["ndof"] != len(case["observations"]["x"]) - 2
                or not math.isclose(
                    metrics["reduced_chi2"],
                    metrics["chi2"] / metrics["ndof"],
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or type(checks) is not dict
                or set(checks) != {"parameters", "covariance", "stationarity"}
                or any(type(value) is not bool for value in checks.values())
                or checks["stationarity"] != (metrics["weighted_normal_residual"] <= 1e-5)
                or (checks["covariance"] and metrics["covariance_normalized_error"] > 1.10001e-7)
                or (expected == "accepted") != all(checks.values())
            ):
                raise invalid
            diagnostic = "not_assessed"
            if expected == "accepted":
                diagnostic = (
                    "above_declared_scale"
                    if metrics["reduced_chi2"] > 3
                    else "below_declared_scale"
                    if metrics["reduced_chi2"] < 0.25
                    else "within_declared_scale"
                )
            if row["model_diagnostic"] != diagnostic:
                raise invalid
    return copy.deepcopy(result)


def verified_evidence(job):
    """Trusted completion/attempt evidence, without implying cross-type ranking."""
    try:
        if not isinstance(job.version.data, dict):
            raise InvalidResult("Invalid problem version")
        if job.version.data.get("evaluator") not in SPECIALISTS:
            return verified_metrics(job)
        metrics = _verified(job, specialist=True)
        specialist_feedback(job.version, job.result, job.mode)
        assessment = job.result.get("assessment")
        if job.version.data["evaluator"] == "python-agent-tools-v1":
            from judge.agent import ASSESSMENT
            from judge.agent_policy import profile_digest

            assessment = {
                **ASSESSMENT,
                "metric_version": "ledger-tools-v1",
                "profile_digest": profile_digest(),
                "visibility": "public-cases-only",
            }
        return {**metrics, "assessment": copy.deepcopy(assessment)}
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InvalidResult("Incomplete or incompatible formal result evidence") from None


def is_formal_evidence(job):
    try:
        verified_evidence(job)
    except (InvalidResult, TypeError, ValueError, KeyError):
        return False
    return True


def safe_result(job):
    if job.version.data.get("evaluator") not in SPECIALISTS:
        return job.result
    try:
        return specialist_feedback(job.version, job.result, job.mode)
    except (InvalidResult, TypeError, ValueError, KeyError):
        return {}


def evidence_jobs(user):
    """Stream only this user's candidate evidence; never load submitted source."""
    from core.models import Submission

    return (
        Submission.objects.filter(user=user, mode="submit", status="finished", verdict__in=VERDICTS)
        .select_related("version")
        .defer("source")
        .order_by("-finished_at", "-pk")
    )
