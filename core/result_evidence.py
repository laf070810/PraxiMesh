"""Read-only checks for the persisted sequential-test result contracts.

These checks reject incomplete/mismatched records. They do not authenticate
client-uploaded results; only the trusted worker may produce formal evidence.
"""

import hashlib
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


def verified_metrics(job):
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
    evaluator = job.version.data.get("evaluator")
    language = SEQUENTIAL_EVALUATORS.get(evaluator)
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
    elif policy == "wasi-python-function-v1" and evaluator == "python-function-v1":
        backend, artifact = "wasi", environment.get("bundle")
    elif policy == "gvisor-python-function-v1" and evaluator == "python-function-v1":
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
