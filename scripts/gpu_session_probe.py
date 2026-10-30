"""Reviewed real-device checks for the production-intended CUDA session path."""

import hashlib
import json
import time
from pathlib import Path

from judge.gpu import evaluate_kernel
from judge.gpu_kernel_checks import problem_for
from judge.gpu_policy import PROFILE, execution_digest, profile_digest
from judge.gpu_probes import VECTOR_REFERENCE, correctness_fixtures, fault_fixtures
from judge.sandbox import SandboxUnavailable


def verify_sessions(factory, report, save):
    root = Path(__file__).resolve().parents[1]
    report["session_profile"] = PROFILE
    report["session_profile_digest"] = profile_digest()
    report["execution_digest"] = execution_digest()
    report["session_sources"] = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in [
            "judge/gpu.py",
            "judge/gpu_session.py",
            "judge/gpu_policy.py",
            "scripts/gpu_session_probe.py",
        ]
    }
    report["session_probes"] = []

    def record(name, **extra):
        report["session_probes"].append({"name": name, "passed": True, **extra})
        save()
        print(json.dumps({"phase": "session_probe_passed", "name": name}), flush=True)

    def grade(fixture, expected, *, mode="submit", name=None):
        name = name or fixture["name"]
        problem = problem_for(fixture)
        with factory(name, mode=mode, on_tick=lambda: None) as session:
            result = evaluate_kernel(problem, fixture["source"], mode, session)
        if result["verdict"] != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {result['verdict']}")
        if any("protected-case" in row["name"] for row in result["public_cases"]):
            raise RuntimeError("Session result disclosed protected test labels")
        if expected == "accepted":
            if result["passed"] != result["total"]:
                raise RuntimeError("Reference did not pass all selected cases")
            if mode == "submit":
                performance = result["performance"]
                if len(performance["samples_ms"]) != PROFILE["sample_batches"]:
                    raise RuntimeError("Missing measured benchmark samples")
                if not all(value > 0 for value in performance["samples_ms"]):
                    raise RuntimeError("Invalid controller wall times")
            elif "performance" in result or result["total"] != 1:
                raise RuntimeError("Run must select only public cases and produce no performance")
        elif "performance" in result:
            raise RuntimeError("Failed correctness received a performance result")
        record(name, result=result)

    fixtures = correctness_fixtures()
    for fixture in fixtures:
        expected = {2: "runtime_error", 3: "syntax_error"}.get(
            fixture["exit_code"], "accepted" if fixture["correct"] else "wrong_answer"
        )
        grade(fixture, expected)
    vector = fixtures[0]
    grade(vector, "accepted", mode="run", name="public-run")
    maximum = dict(
        vector,
        name="maximum-vector",
        inputs=[
            {"a": [float(i % 19 - 9) / 4 for i in range(4096)], "b": [0.25] * 4096},
            {"a": [0.5] * 4096, "b": [float(i % 13 - 6) / 2 for i in range(4096)]},
        ],
    )
    grade(maximum, "accepted")
    matrix = next(f for f in fixtures if f["name"] == "matrix-reference")
    grade(
        dict(
            matrix,
            name="maximum-matrix",
            inputs=[
                matrix["inputs"][0],
                {
                    "m": 64,
                    "n": 64,
                    "k": 64,
                    "a": [float(i % 7 - 3) / 4 for i in range(4096)],
                    "b": [float(i % 11 - 5) / 2 for i in range(4096)],
                },
            ],
        ),
        "accepted",
    )
    faults = {fixture["name"]: fixture for fixture in fault_fixtures()}
    for name, verdict in [
        ("printf-spoof", "accepted"),
        ("illegal-address", "runtime_error"),
        ("module-memory-limit", "memory_limit"),
        ("kernel-timeout", "time_limit"),
    ]:
        grade(faults[name], verdict)
        if verdict != "accepted":
            grade(vector, "accepted", mode="run", name="session-recover-" + name)
    flood = VECTOR_REFERENCE.replace(
        "if (i < n)",
        'if (i == 0) for(int j=0; j<4096; ++j) printf("device-output-limit-canary\\n");\n'
        "    if (i < n)",
    )
    grade(dict(faults["printf-spoof"], name="stderr-budget", source=flood), "output_limit")
    grade(vector, "accepted", mode="run", name="recover-stderr-budget")

    class Cancelled(Exception):
        pass

    for action in ("cancel", "attach-death"):
        state = {"armed": False, "session": None}

        def tick(action=action, state=state):
            if state["armed"]:
                if action == "cancel":
                    raise Cancelled
                state["session"].process.kill()  # Only this fixture's attach client.

        started = time.monotonic()
        try:
            with factory(action, mode="run", on_tick=tick) as session:
                state["session"] = session
                session.compile(faults["kernel-timeout"]["source"], vector["operation"])
                state["armed"] = True
                session.execute({"inputs": faults["kernel-timeout"]["inputs"][0]})
            raise RuntimeError("A cancelled session unexpectedly returned a result")
        except Cancelled:
            if action != "cancel":
                raise
        except SandboxUnavailable:
            if action != "attach-death":
                raise
        cleanup = report["session_cleanup"][-1]
        if cleanup["name"] != action or not cleanup["running_before_cleanup"]:
            raise RuntimeError("Cancellation fixture did not require physical container cleanup")
        record(action, elapsed_seconds=time.monotonic() - started)
        grade(vector, "accepted", mode="run", name="recover-" + action)
