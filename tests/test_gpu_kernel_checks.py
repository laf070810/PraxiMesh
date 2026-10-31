"""Catalog/protocol checks and fixed CPU pipes; no CUDA or isolation acceptance."""

import copy
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from judge import gpu_kernel_checks as checks
from judge.gpu_contract import validate_problem
from judge.gpu_policy import PROFILE, profile_digest
from judge.gpu_session import GPUDriverExited, GPUExecutionError, GPUSession
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable

REQUEST = checks.check_request("public-run", "a" * 32)


def check_report(request=REQUEST):
    """Synthetic IPC data only; never written to a GPU startup receipt."""
    name = request["name"]
    report = {
        "name": name,
        "challenge": request["challenge"],
        "cleanup": {
            "id": "a" * 64,
            "running_before_cleanup": True,
            "exit_code": 4 if name == "module-memory-limit" else 0,
            "stopped": True,
        },
    }
    if name in checks.INTERRUPTIONS:
        report["interruption"] = name
        return report
    case = checks.case_for(name)
    verdict = case["verdict"]
    total = len(case["problem"]["tests"]) if case["mode"] == "submit" else 1
    result = {
        "verdict": verdict,
        "label": LABELS[verdict],
        "passed": total if verdict == "accepted" else 0,
        "total": total,
        "public_cases": [],
    }
    if case["compiled"]:
        result["compile_initialization_ms"] = 1.0
    if verdict in {"accepted", "wrong_answer"}:
        result["public_cases"] = [{"name": "public", "verdict": verdict, "label": LABELS[verdict]}]
    if verdict == "accepted" and case["mode"] == "submit":
        result["performance"] = {
            "profile": PROFILE["id"],
            "profile_digest": profile_digest(),
            "measurement": PROFILE["measurement"],
            "warmup_batches": PROFILE["warmup_batches"],
            "sample_batches": PROFILE["sample_batches"],
            "batch_repetitions": PROFILE["batch_repetitions"],
            "samples_ms": [1.0] * PROFILE["sample_batches"],
            "median_batch_ms": 1.0,
        }
    report["result"] = result
    return report


@pytest.mark.parametrize("name", checks.CHECKS)
def test_fixed_catalog_and_matching_report(name):
    request = checks.check_request(name, "b" * 32)
    assert set(request) == {"event", "name", "challenge"}
    assert len(checks.CHECKS) == len(set(checks.CHECKS)) == 19
    if name not in checks.INTERRUPTIONS:
        case = checks.case_for(name)
        validate_problem(case["problem"])
        assert case["source"] and case["problem"]["language"] == "cuda"
    checks.validate_check_report(check_report(request), request)


def test_run_has_nonempty_input_and_catalog_is_not_shared_mutable_state():
    case = checks.case_for("public-run")
    assert case["problem"]["tests"][0]["inputs"]["a"]
    case["problem"]["tests"][0]["inputs"]["a"].clear()
    assert checks.case_for("public-run")["problem"]["tests"][0]["inputs"]["a"]


@pytest.mark.parametrize("name", [None, [], True, "arbitrary-command", "../vector-reference"])
def test_unknown_check_is_rejected(name):
    with pytest.raises(ValueError):
        checks.check_request(name, "a" * 32)


@pytest.mark.parametrize(
    "defect", ["challenge", "extra", "cleanup", "bool", "identity", "verdict", "hidden", "compile"]
)
def test_incomplete_or_unmatched_report_is_not_a_success(defect):
    value = check_report()
    if defect == "challenge":
        value["challenge"] = "b" * 32
    elif defect == "extra":
        value["ready"] = True
    elif defect == "cleanup":
        value["cleanup"]["stopped"] = False
    elif defect == "bool":
        value["cleanup"]["stopped"] = 1
    elif defect == "identity":
        value["cleanup"]["id"] = "unknown"
    elif defect == "verdict":
        result = value["result"]
        result.update(verdict="wrong_answer", label=LABELS["wrong_answer"], passed=0)
    elif defect == "hidden":
        value["result"]["public_cases"][0]["name"] = "protected-case-1"
    else:
        del value["result"]["compile_initialization_ms"]
    with pytest.raises(SandboxUnavailable):
        checks.validate_check_report(value, REQUEST)


@pytest.mark.parametrize("name", ["illegal-address", "kernel-timeout", "stderr-budget"])
def test_setup_failure_cannot_pass_an_execution_fault_check(name):
    request = checks.check_request(name, "a" * 32)
    report = check_report(request)
    del report["result"]["compile_initialization_ms"]
    with pytest.raises(SandboxUnavailable, match="compilation"):
        checks.validate_check_report(report, request)


def test_host_oom_cannot_substitute_for_cuda_module_memory_failure():
    request = checks.check_request("module-memory-limit", "a" * 32)
    report = check_report(request)
    report["cleanup"]["exit_code"] = 137
    with pytest.raises(SandboxUnavailable, match="driver memory failure"):
        checks.validate_check_report(report, request)


@pytest.mark.parametrize("samples", [False, True])
def test_submit_requires_positive_samples_and_run_never_has_performance(samples):
    request = checks.check_request("vector-reference", "a" * 32)
    report = check_report(request)
    if samples:
        report["result"]["performance"].update(
            samples_ms=[0.0] * PROFILE["sample_batches"], median_batch_ms=0.0
        )
        with pytest.raises(SandboxUnavailable, match="positive"):
            checks.validate_check_report(report, request)
    else:
        run = check_report()
        run["result"]["performance"] = report["result"]["performance"]
        with pytest.raises(SandboxUnavailable, match="fully correct Submit"):
            checks.validate_check_report(run, REQUEST)


@pytest.fixture
def runtime(monkeypatch):
    events = []
    state = SimpleNamespace(cleanup=check_report()["cleanup"], error=None)

    class Session:
        mode = "run"

        def cleanup(self):
            events.append("cleanup")
            return copy.deepcopy(state.cleanup)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.cleanup()

    def evaluate(problem, source, mode, session):
        case = checks.case_for("public-run")
        assert problem == case["problem"] and source == case["source"] and mode == "run"
        events.append("evaluate")
        if state.error:
            raise state.error
        return check_report()["result"]

    monkeypatch.setattr(checks, "evaluate_kernel", evaluate)
    value = SimpleNamespace(
        tick=lambda: events.append("tick"),
        kernel_session=lambda name, **_kwargs: Session(),
        containers={"startup-check": {"id": "a" * 64}},
    )
    return value, state, events


def test_check_captures_actual_cleanup_before_return(runtime):
    value, _state, events = runtime
    assert checks.check_kernel(value, REQUEST) == check_report()
    assert events.index("evaluate") < events.index("cleanup") < len(events) - 1


def test_cleanup_cannot_refer_to_a_different_container(runtime):
    value, state, _events = runtime
    state.cleanup["id"] = "b" * 64
    with pytest.raises(SandboxUnavailable, match="changed the owned container"):
        checks.check_kernel(value, REQUEST)


def test_platform_failure_propagates_after_cleanup(runtime):
    value, state, events = runtime
    state.error = SandboxUnavailable("fixed platform failure")
    with pytest.raises(SandboxUnavailable, match="platform failure"):
        checks.check_kernel(value, REQUEST)
    assert "cleanup" in events


def test_check_does_not_accept_supplied_source(runtime):
    value, _state, events = runtime
    with pytest.raises(SandboxUnavailable, match="Unexpected"):
        checks.check_kernel(value, {**REQUEST, "source": "must not run"})
    assert events == []


# This fixed CPU helper emits a compiled frame then blocks. It neither compiles
# nor executes incoming CUDA source, and is not evidence of container cleanup.
PIPE_HELPER = """
import json, sys, time
request = sys.stdin.readline()
print(json.dumps({'event': 'compiled', 'architecture': [8, 6]}), flush=True)
request = sys.stdin.readline()
time.sleep(30)
"""


@pytest.mark.parametrize("name", sorted(checks.INTERRUPTIONS))
@pytest.mark.parametrize("observe", [False, True])
def test_interruption_uses_real_owned_cpu_pipe_and_finds_driver_exit(name, observe):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", PIPE_HELPER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cleaned = []
    observations = []
    try:
        with GPUSession(
            process,
            lambda: cleaned.append(True),
            architecture=[8, 6],
            mode="run",
            on_tick=lambda: None,
        ) as session:
            started = time.monotonic()

            def before_interrupt():
                assert session.compiled and process.poll() is None
                observations.append(True)

            assert (
                checks.interrupt_session(
                    SimpleNamespace(tick=lambda: None),
                    session,
                    name,
                    before_interrupt=before_interrupt if observe else None,
                )
                == name
            )
            assert session.compiled and time.monotonic() - started >= 0.5
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    assert process.returncode < 0 and cleaned == [True]
    assert observations == ([True] if observe else [])


@pytest.mark.parametrize("name", sorted(checks.INTERRUPTIONS))
@pytest.mark.parametrize(
    "error",
    [
        GPUDriverExited("early exit"),
        GPUExecutionError("time_limit"),
        SandboxUnavailable("disconnect"),
    ],
)
def test_failure_before_injection_is_not_expected_interruption(name, error):
    class Session:
        def compile(self, *_args):
            pass

        def execute(self, _case):
            raise error

    with pytest.raises(type(error)):
        checks.interrupt_session(SimpleNamespace(tick=lambda: None), Session(), name)


@pytest.mark.parametrize("name", sorted(checks.INTERRUPTIONS))
def test_stopped_container_does_not_prove_interruption_cleanup(name):
    request = checks.check_request(name, "a" * 32)
    value = check_report(request)
    value["cleanup"]["running_before_cleanup"] = False
    with pytest.raises(SandboxUnavailable, match="physical container cleanup"):
        checks.validate_check_report(value, request)


def test_fixed_catalog_never_contains_serialized_expected_results():
    request = checks.check_request("vector-reference", "a" * 32)
    assert "expected" not in json.dumps(request) and "source" not in request
