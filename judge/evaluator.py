from judge.results import LABELS, matches


def python_function_v1(version, source, mode, sandbox, on_case):
    return function_v1(version, source, mode, sandbox.execute, on_case)


def javascript_function_v1(version, source, mode, sandbox, on_case):
    return function_v1(version, source, mode, sandbox.execute_javascript, on_case)


def python_stdio_v1(version, source, mode, sandbox, on_case):
    if version.get("output_format") != "json":
        raise ValueError("Unsupported program output format")
    return function_v1(version, source, mode, sandbox.execute_program, on_case, program=True)


def function_v1(version, source, mode, execute, on_case, program=False):
    cases = [case for case in version["tests"] if mode == "submit" or case["public"]]
    passed = 0
    elapsed = 0
    feedback = []
    verdict = "accepted"
    for case in cases:
        on_case()
        result = (
            execute(source, case["stdin"])
            if program
            else execute(source, version["function"], case["args"])
        )
        elapsed += result.elapsed_ms
        outcome = result.verdict
        if outcome == "ok":
            outcome = (
                "accepted"
                if matches(result.value, case["expected"], version["comparison"])
                else "wrong_answer"
            )
        if case["public"]:
            feedback.append({"name": case["name"], "verdict": outcome, "label": LABELS[outcome]})
        if outcome != "accepted":
            verdict = outcome
            break
        passed += 1
    # Never return hidden labels, inputs, outputs, stderr, or exception text.
    return {
        "verdict": verdict,
        "label": LABELS[verdict],
        "passed": passed,
        "total": len(cases),
        "elapsed_ms": elapsed,
        "public_cases": feedback,
    }


# Reviewed server implementations only; content never chooses an import path.
EVALUATORS = {
    "python-function-v1": python_function_v1,
    "javascript-function-v1": javascript_function_v1,
    "python-stdio-v1": python_stdio_v1,
}


def evaluate(version, source, mode, sandbox, on_case=lambda: None):
    evaluator = EVALUATORS.get(version["evaluator"])
    if evaluator is None:
        raise ValueError("Unsupported evaluator")
    if mode not in {"run", "submit"}:
        raise ValueError("Unsupported evaluation mode")
    return evaluator(version, source, mode, sandbox, on_case)
