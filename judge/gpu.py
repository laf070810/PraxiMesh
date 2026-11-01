"""Controller correctness and batch timing, not yet registered for admission."""

import statistics

from judge.gpu_contract import input_shape, output_matches, validate_problem
from judge.gpu_policy import PROFILE, profile_digest
from judge.gpu_session import GPUExecutionError
from judge.results import LABELS


def evaluate_kernel(problem, source, mode, session):
    validate_problem(problem)
    if mode not in {"run", "submit"} or session.mode != mode:
        raise ValueError("Unknown GPU evaluation mode")
    return _evaluate(problem, source, mode, session)


def evaluate_public_kernel(problem, source, session):
    """Public examples use the same trusted comparison, without Submit timing."""
    from judge.gpu_task_protocol import public_task_request

    public_task_request(problem, source)
    if session.mode != "run":
        raise ValueError("Public GPU examples require a Run session")
    return _evaluate(problem, source, "run", session)


def _evaluate(problem, source, mode, session):
    cases = [case for case in problem["tests"] if mode == "submit" or case["public"]]
    result = {"passed": 0, "total": len(cases), "public_cases": []}
    verdict = "accepted"
    try:
        result["compile_initialization_ms"] = session.compile(source, problem["operation"])
        for case in cases:
            values, _elapsed = session.execute(case)
            correct = output_matches(values, case["expected"], problem["comparison"])
            result["passed"] += correct
            current = "accepted" if correct else "wrong_answer"
            if not correct:
                verdict = current
            if case["public"]:
                result["public_cases"].append(
                    {"name": case["name"], "verdict": current, "label": LABELS[current]}
                )
        if mode == "submit" and verdict == "accepted":
            case = max(
                cases,
                key=lambda item: (
                    input_shape(problem["operation"], item["inputs"])[0]
                    * item["inputs"].get("k", 1)
                ),
            )
            samples = []
            for index in range(PROFILE["warmup_batches"] + PROFILE["sample_batches"]):
                values, elapsed = session.execute(case, batch=True)
                if not output_matches(values, case["expected"], problem["comparison"]):
                    verdict = "wrong_answer"
                    break
                if index >= PROFILE["warmup_batches"]:
                    samples.append(elapsed)
            if verdict == "accepted":
                result["performance"] = {
                    "profile": PROFILE["id"],
                    "profile_digest": profile_digest(),
                    "measurement": PROFILE["measurement"],
                    "warmup_batches": PROFILE["warmup_batches"],
                    "sample_batches": PROFILE["sample_batches"],
                    "batch_repetitions": PROFILE["batch_repetitions"],
                    "samples_ms": samples,
                    "median_batch_ms": statistics.median(samples),
                }
        session.finish()
    except GPUExecutionError as error:
        verdict = error.verdict
    if verdict != "accepted":
        result.pop("performance", None)
    result.update(verdict=verdict, label=LABELS[verdict])
    return result
