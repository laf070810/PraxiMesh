"""Real JS execution requires explicit WASI artifacts; unit checks never execute code."""

import io
import json
import os
from pathlib import Path

import pytest
from django.core.management import call_command

from core.management.commands.sandbox_check import check_receipt, probes_for, suite_digest
from judge.backends import configured_sandbox
from judge.evaluator import evaluate
from judge.javascript import validate_request
from judge.javascript_probes import PROBES
from judge.sandbox import DockerSandbox, SandboxUnavailable
from judge.wasi_policy import POLICY_VERSION

REAL = pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
    reason="Requires the real pinned dual-language WASI bundle; skipped is not evidence",
)


@pytest.mark.parametrize(
    "args",
    [
        [2**53],
        [10**1000],
        [-(2**53)],
        [float("inf")],
        [float("nan")],
        [b"bytes"],
        [{1: "key"}],
        [set()],
    ],
)
def test_javascript_request_rejects_non_json_or_unsafe_numbers(args):
    with pytest.raises(ValueError):
        validate_request("function solve() {}", "solve", args)


def test_javascript_request_depth_and_identifier_limits():
    value = 0
    for _ in range(33):
        value = [value]
    with pytest.raises(ValueError):
        validate_request("function solve() {}", "solve", value)
    with pytest.raises(ValueError):
        validate_request("function solve() {}", "solve(); other()", [])
    validate_request("function solve() {}", "$solve_1", [[-(2**53 - 1), 2**53 - 1, 0.25]])


def test_gvisor_explicitly_rejects_javascript_without_execution(monkeypatch):
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported JavaScript must not start any container")

    monkeypatch.setattr(sandbox, "command", forbidden)
    with pytest.raises(SandboxUnavailable, match="JavaScript.*WASI"):
        sandbox.execute_javascript("function solve() {}", "solve", [])


def test_joint_receipt_requires_both_languages(settings, tmp_path):
    # Receipt verification only; no claim that this fixture proves isolation.
    identity = {"policy": POLICY_VERSION, "bundle": "a" * 64}
    probes = probes_for(identity)
    assert {item[4] for item in probes} == {"python", "javascript", "python-stdio"}
    assert len({item[0] for item in probes}) == len(probes)
    import time

    path = tmp_path / "receipt.json"
    settings.JUDGE_RECEIPT = str(path)
    receipt = {
        "created": time.time(),
        "identity": identity,
        "suite": suite_digest(identity),
        "passed": [p[0] for p in probes if p[4] == "python"],
    }
    path.write_text(json.dumps(receipt))
    with pytest.raises(SandboxUnavailable):
        check_receipt(identity)
    with pytest.raises(SandboxUnavailable, match="Unknown isolation"):
        probes_for({"policy": "wasi-python-function-v1"})


@REAL
@pytest.mark.sandbox
def test_actual_javascript_boundary_probes():
    sandbox = configured_sandbox()
    environment = sandbox.preflight()
    assert environment["languages"] == ["python", "javascript"]
    for name, source, verdict, expected in PROBES:
        result = sandbox.execute_javascript(source, "solve", [])
        assert result.verdict == verdict, (name, result)
        if verdict == "ok":
            assert type(result.value) is type(expected) and result.value == expected, name


@REAL
@pytest.mark.sandbox
def test_actual_javascript_correct_wrong_and_hidden_feedback():
    sandbox = configured_sandbox()
    sandbox.preflight()
    problem = {
        "evaluator": "javascript-function-v1",
        "function": "solve",
        "comparison": {"kind": "exact", "absolute": 0, "relative": 0},
        "tests": [
            {"name": "public", "public": True, "args": [[-3, 2, 0, 5]], "expected": 7},
            {"name": "private-case-name", "public": False, "args": [[-4, 0]], "expected": 0},
        ],
    }
    correct = "function solve(a) { return a.filter(x => x > 0).reduce((a,b) => a+b, 0); }"
    wrong = "function solve(a) { return a.reduce((a,b) => a+b, 0); }"
    result = evaluate(problem, correct, "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == 2
    assert "private-case-name" not in json.dumps(result)
    assert evaluate(problem, wrong, "submit", sandbox)["verdict"] == "wrong_answer"
    assert evaluate(problem, correct, "run", sandbox)["total"] == 1
    forged = 'function solve() { return {verdict:"accepted", score:100}; }'
    assert evaluate(problem, forged, "submit", sandbox)["verdict"] == "wrong_answer"
    # Same sandbox instance retains the original Python call interface.
    assert sandbox.execute("def solve(a): return a + 1", "solve", [4]).value == 5


@REAL
@pytest.mark.sandbox
def test_actual_javascript_parameters_are_values_and_host_canary_is_private(tmp_path):
    sandbox = configured_sandbox()
    sandbox.preflight()
    payload = {
        "quote": '"); globalThis.injected = true; //',
        "unicode": "中😀\ud800",
        "nested": [1, None, False],
    }
    result = sandbox.execute_javascript("function solve(x) { return x; }", "solve", [payload])
    assert result.verdict == "ok" and result.value == payload
    canary = tmp_path / "private-host-canary"
    canary.write_text("not supplied to the guest")
    source = "function solve(path) { try { return require('fs').readFileSync(path,'utf8'); } catch(e) { return typeof std === 'undefined' && typeof os === 'undefined'; } }"
    result = sandbox.execute_javascript(source, "solve", [str(canary)])
    assert result.verdict == "ok" and result.value is True
    assert canary.read_text() == "not supplied to the guest"


@REAL
@pytest.mark.sandbox
def test_actual_joint_receipt(settings, tmp_path):
    settings.JUDGE_RECEIPT = str(tmp_path / "receipt.json")
    call_command("sandbox_check", stdout=io.StringIO())
    sandbox = configured_sandbox()
    identity = sandbox.preflight()
    check_receipt(identity)
    receipt = json.loads(Path(settings.JUDGE_RECEIPT).read_text())
    assert receipt["passed"] == [p[0] for p in probes_for(identity)]
    assert any(name.startswith("javascript/") for name in receipt["passed"])


@REAL
@pytest.mark.sandbox
def test_actual_rapid_driver_exit_keeps_native_callbacks_out_of_python_shutdown():
    sandbox = configured_sandbox()
    sandbox.preflight()
    # Regression: short-lived native WASI callback threads raced Python shutdown.
    # These are real child processes and real guest executions, not mocks.
    for _ in range(5):
        assert (
            sandbox.execute_javascript("function solve() { return 1; }", "solve", []).verdict
            == "ok"
        )
        assert (
            sandbox.execute_javascript(
                "function solve() { throw new Error(); }", "solve", []
            ).verdict
            == "runtime_error"
        )
        assert (
            sandbox.execute_javascript(
                "function solve() { return new ArrayBuffer(100*1024*1024); }", "solve", []
            ).verdict
            == "memory_limit"
        )


@REAL
@pytest.mark.sandbox
def test_actual_javascript_protocol_input_and_output_boundaries():
    from judge.wasi_policy import INPUT, OUTPUT

    sandbox = configured_sandbox()
    sandbox.preflight()
    source = "function solve() { return 1; } //"
    empty = json.dumps(
        {"source": source, "function": "solve", "args": []}, ensure_ascii=True
    ).encode()
    source += "x" * (INPUT - len(empty))
    assert sandbox.execute_javascript(source, "solve", []).value == 1
    with pytest.raises(SandboxUnavailable, match="Input exceeds"):
        sandbox.execute_javascript(source + "x", "solve", [])
    # The envelope and JSON quotes occupy 12 bytes; the entire transport is bounded.
    result = sandbox.execute_javascript(
        "function solve(n) { return 'x'.repeat(n); }", "solve", [OUTPUT - 12]
    )
    assert result.verdict == "ok" and len(result.value) == OUTPUT - 12
    assert (
        sandbox.execute_javascript(
            "function solve(n) { return 'x'.repeat(n); }", "solve", [OUTPUT - 11]
        ).verdict
        == "output_limit"
    )
