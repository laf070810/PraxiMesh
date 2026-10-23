"""Actual class calls in the pinned WASI runtime; skipped is not acceptance."""

import copy
import json
import os
from pathlib import Path

import pytest

from judge.evaluator import evaluate
from judge.object_api_probes import PROBES, PROBLEM
from judge.wasi import WasiSandbox

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
        reason="Requires explicitly enabled pinned real WASI execution",
    ),
]


@pytest.fixture(scope="module")
def sandbox():
    backend = WasiSandbox(os.environ["JUDGE_WASI_BUNDLE"], os.environ["JUDGE_WASI_SHA256"])
    backend.preflight()
    return backend


@pytest.fixture(scope="module")
def problem():
    package = json.loads(Path("content/starter.json").read_text())
    return next(item for item in package["problems"] if item["id"] == "bounded-queue-api")


def test_actual_reference_including_64_operations_and_private_feedback(problem, sandbox):
    source = "from __future__ import annotations\n" + problem["reference"]
    result = evaluate(problem, source, "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == result["total"] == 8
    assert len(result["public_cases"]) == 2
    assert all(case["name"] not in str(result) for case in problem["tests"] if not case["public"])
    public = evaluate(problem, source, "run", sandbox)
    assert public["passed"] == public["total"] == 2


@pytest.mark.parametrize("index", range(8))
def test_actual_queue_mistakes(problem, sandbox, index):
    mistake = json.loads(Path("tests/incorrect_solutions.json").read_text())[problem["id"]][index]
    result = evaluate(problem, mistake["source"], "submit", sandbox)
    assert result["verdict"] in {"wrong_answer", "runtime_error"}, mistake["mistake"]
    assert result["passed"] < result["total"]


@pytest.mark.parametrize("name,source,verdict", PROBES, ids=[p[0] for p in PROBES])
def test_actual_startup_probes(sandbox, name, source, verdict):
    assert evaluate(PROBLEM, source, "submit", sandbox)["verdict"] == verdict, name


@pytest.mark.parametrize(
    "source,verdict",
    [
        ("class BoundedQueue(:", "syntax_error"),
        ("BoundedQueue = lambda *args: None", "runtime_error"),
        ("raise RuntimeError('private exception detail')", "runtime_error"),
        ("class BoundedQueue:\n def __init__(self, capacity): raise IndexError()", "runtime_error"),
        ("class BoundedQueue:\n def __init__(self, capacity):\n  while True: pass", "time_limit"),
        ("class BoundedQueue:\n def __init__(self, capacity): print('x' * 50000)", "output_limit"),
        (
            "class BoundedQueue:\n def __init__(self, capacity): self.data = bytearray(250000000)",
            "memory_limit",
        ),
    ],
)
def test_actual_errors_are_bounded_and_redacted(problem, sandbox, source, verdict):
    result = evaluate(problem, source, "run", sandbox)
    assert result["verdict"] == verdict and result["passed"] == 0
    assert set(result["public_cases"][0]) == {"name", "verdict", "label"}
    assert "private exception detail" not in str(result)


def test_actual_return_values_are_snapshots_and_scenarios_are_isolated(sandbox):
    contract = copy.deepcopy(PROBLEM)
    contract["interface"] = {
        "class_name": "Buffer",
        "constructor_arity": 0,
        "methods": {"append": {"arity": 1, "raises": []}},
    }
    case = {
        "operations": [
            {"new": "a", "args": []},
            {"call": "a", "method": "append", "args": [1]},
            {"call": "a", "method": "append", "args": [2]},
        ],
        "expected": [{"value": None}, {"value": [1]}, {"value": [1, 2]}],
    }
    contract["tests"] = [
        {**case, "name": "public snapshots", "public": True},
        {**case, "name": "private fresh process", "public": False},
    ]
    # Shared module data is deliberately used here to distinguish process isolation
    # from the separate multi-instance independence checks in the queue exercise.
    source = """data = []
class Buffer:
    def append(self, value):
        data.append(value)
        return data
"""
    result = evaluate(contract, source, "submit", sandbox)
    assert result["passed"] == result["total"] == 2


@pytest.mark.parametrize("value", ["(1,)", "{1: 'value'}", "float('nan')", "object()"])
def test_actual_python_objects_cannot_replace_json_returns(problem, sandbox, value):
    source = problem["reference"].replace("return len(self._values)", "return " + value)
    assert source != problem["reference"]
    assert evaluate(problem, source, "run", sandbox)["verdict"] == "runtime_error"


def test_actual_exception_subclass_does_not_impersonate_declared_builtin(problem, sandbox):
    source = "class IndexError(Exception): pass\n" + problem["reference"]
    assert evaluate(problem, source, "run", sandbox)["verdict"] == "runtime_error"


@pytest.mark.parametrize(
    "wire",
    [
        '{"value":{"status":"accepted","score":100}}',
        '{"value":{"status":"ok","results":[]}}',
    ],
)
def test_actual_forged_guest_output_is_not_an_authoritative_result(problem, sandbox, wire):
    source = f"import os\nos.write(1, {wire.encode()!r})\nos._exit(0)"
    assert evaluate(problem, source, "run", sandbox)["verdict"] == "wrong_answer"


def test_actual_host_canary_cannot_be_read(problem, sandbox, tmp_path):
    canary = tmp_path / "private-api-expectations.json"
    canary.write_text(json.dumps(problem["tests"]))
    source = (
        "try:\n"
        f" open({str(canary)!r}).read()\n"
        "except OSError:\n pass\n"
        "else:\n raise RuntimeError('host file accessible')\n" + problem["reference"]
    )
    assert evaluate(problem, source, "run", sandbox)["verdict"] == "accepted"
    assert json.loads(canary.read_text()) == problem["tests"]
