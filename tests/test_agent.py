"""Protocol checks and explicitly marked real WASI agent execution evidence."""

import copy
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from judge.agent import (
    TASK,
    Ledger,
    ProtocolError,
    evaluate_public,
    project_public_problem,
    python_agent_tools_v1,
    validate_cases,
    validate_public_problem,
    validate_state,
)
from judge.agent_policy import PROFILE, profile_digest
from judge.agent_probes import PROBES, PROBLEM, REFERENCE
from judge.agent_runtime import AgentRuntime, EpisodeBudget
from judge.backends import configured_sandbox
from judge.sandbox import DockerSandbox, SandboxUnavailable

REAL = pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
    reason="Requires actual pinned WASI artifacts; skipped is not execution evidence",
)


@pytest.fixture
def problem():
    return copy.deepcopy(PROBLEM)


@pytest.fixture
def sandbox():
    instance = configured_sandbox()
    instance.preflight()
    return instance


@pytest.mark.parametrize(
    "change",
    [
        "language",
        "function",
        "profile",
        "assessment-extra",
        "all-public",
        "all-hidden",
        "no-cases",
        "many-cases",
        "case-extra",
        "visibility-int",
        "duplicate-name",
        "environment-extra",
        "empty-records",
        "many-records",
        "record-extra",
        "duplicate-id",
        "invalid-id",
        "bool-amount",
        "float-amount",
        "huge-amount",
        "int-approved",
    ],
)
def test_formal_agent_contract_rejects_ambiguous_inputs(problem, change):
    case = problem["tests"][0]
    records = case["environment"]["records"]
    if change == "language":
        problem["language"] = "javascript"
    elif change == "function":
        problem["function"] = "solve"
    elif change == "profile":
        problem["assessment"]["resource_profile"] = "unbounded"
    elif change == "assessment-extra":
        problem["assessment"]["allow_hidden_omission"] = True
    elif change in {"all-public", "all-hidden"}:
        for item in problem["tests"]:
            item["public"] = change == "all-public"
    elif change == "no-cases":
        problem["tests"] = []
    elif change == "many-cases":
        problem["tests"] *= 5
    elif change == "case-extra":
        case["expected"] = {"success": True}
    elif change == "visibility-int":
        case["public"] = 1
    elif change == "duplicate-name":
        problem["tests"][1]["name"] = case["name"]
    elif change == "environment-extra":
        case["environment"]["tool_import"] = "os.system"
    elif change == "empty-records":
        records.clear()
    elif change == "many-records":
        records.append({"id": "fifth", "amount": 1, "approved": True})
    elif change == "record-extra":
        records[0]["path"] = "/etc/passwd"
    elif change == "duplicate-id":
        records[1]["id"] = records[0]["id"]
    elif change == "invalid-id":
        records[0]["id"] = "../other-user"
    elif change == "bool-amount":
        records[0]["amount"] = True
    elif change == "float-amount":
        records[0]["amount"] = 1.0
    elif change == "huge-amount":
        records[0]["amount"] = 100001
    else:
        records[0]["approved"] = 1
    with pytest.raises(ValueError):
        validate_cases(problem)


def test_public_projection_is_exact_and_cannot_authorize_formal_submit(problem):
    problem["reference"] = "private reference"
    problem["solution"] = "private solution"
    public = project_public_problem(problem)
    assert validate_public_problem(public) is public
    assert set(public) == {"evaluator", "language", "function", "assessment", "tests"}
    assert "hidden" not in json.dumps(public) and "private reference" not in json.dumps(public)
    public["tests"][0]["environment"]["records"][0]["amount"] = 0
    assert problem["tests"][0]["environment"]["records"][0]["amount"] == 125
    with pytest.raises(ValueError, match="public and hidden"):
        validate_cases(public)
    public["reference"] = "hidden field"
    with pytest.raises(ValueError):
        validate_public_problem(public)
    with pytest.raises(ValueError):
        validate_public_problem(problem)


@pytest.mark.parametrize(
    "state",
    [
        "x" * 129,
        float("nan"),
        float("inf"),
        1_000_000_001,
        {"k" * 33: 1},
        [None] * 129,
        [[[[[[[0]]]]]]],
        {"a": "x" * 128, "b": ["x" * 128] * 8},
        "\ud800",
    ],
)
def test_agent_state_is_bounded_data(state):
    with pytest.raises(ProtocolError, match="invalid_state"):
        validate_state(state)


def test_valid_state_preserves_json_types():
    validate_state({"ids": ["a", "b"], "position": 1, "total": -2, "flag": True, "x": None})


@pytest.mark.parametrize(
    "action,reason",
    [
        ({"tool": "shell", "args": {}, "state": None}, "unauthorized_tool"),
        ({"tool": "list_records", "args": {"path": "x"}, "state": None}, "invalid_arguments"),
        (
            {"tool": "read_record", "args": {"id": "receipt-a"}, "state": None},
            "unauthorized_record",
        ),
        (
            {
                "tool": "submit_report",
                "args": {"total": 105, "count": 3},
                "state": {"listed": True},
            },
            "incomplete_observation",
        ),
        ({"tool": "list_records", "args": {}, "state": None, "score": 100}, "invalid_action"),
        (
            {"tool": "submit_report", "args": {"total": 105, "count": True}, "state": None},
            "invalid_arguments",
        ),
    ],
)
def test_tools_enforce_host_state_and_exact_arguments(problem, action, reason):
    ledger = Ledger(problem["tests"][0]["environment"]["records"])
    with pytest.raises(ProtocolError, match=reason):
        ledger.apply(action, 1)
    assert ledger.report is None and not ledger.trajectory


def test_real_host_transitions_and_report_do_not_use_claimed_trajectory(problem):
    ledger = Ledger(problem["tests"][0]["environment"]["records"])
    listed, _, _ = ledger.apply({"tool": "list_records", "args": {}, "state": None}, 1)
    for index, record_id in enumerate(listed["ids"], start=2):
        ledger.apply({"tool": "read_record", "args": {"id": record_id}, "state": None}, index)
    _, complete, done = ledger.apply(
        {"tool": "submit_report", "args": {"total": 105, "count": 3}, "state": "not in trace"}, 6
    )
    assert complete and done and ledger.report == {"total": 105, "count": 3}
    assert ledger.cost == 7 and len(ledger.trajectory) == 6
    assert "not in trace" not in json.dumps(ledger.trajectory)


def test_fixed_budget_reservations_are_not_claimed_as_measured_fuel():
    budget = EpisodeBudget()
    for _ in range(6):
        assert budget.reserve() == ""
    assert budget.reserve() == "wrong_answer"
    assert budget.fuel_reserved == 15_000_000_000
    assert budget.output_reserved == 24576
    assert budget.cpu_seconds == 0 and budget.exhaustion == "turn_budget"
    assert len(profile_digest()) == 64


def test_agent_rejects_gvisor_without_launching_it():
    with pytest.raises(SandboxUnavailable, match="WASI"):
        AgentRuntime(DockerSandbox("python@sha256:" + "a" * 64))


@REAL
@pytest.mark.sandbox
def test_actual_all_agent_reference_attack_and_resource_probes(sandbox, tmp_path):
    observed = []
    failures = []
    for name, source, verdict in PROBES:
        result = python_agent_tools_v1(PROBLEM, source, "submit", sandbox)
        observed.append({"name": name, "expected": verdict, "result": result})
        (tmp_path / "agent-probes.json").write_text(json.dumps(observed, indent=2))
        if result["verdict"] != verdict:
            failures.append(observed[-1])
        assert "hidden-episode-canary" not in json.dumps(result)
        assert "hidden-record-canary" not in json.dumps(result)
    if failures:
        pytest.fail(json.dumps(failures, indent=2))


@REAL
@pytest.mark.sandbox
def test_actual_six_turn_reference_is_stable_with_live_profile(sandbox):
    public = project_public_problem(PROBLEM)
    for _ in range(3):
        result = evaluate_public(public, REFERENCE, sandbox)
        if result["verdict"] != "accepted":
            pytest.fail(json.dumps(result, indent=2))
        metrics = result["public_cases"][0]["metrics"]
        assert metrics["turns"] == 6 and metrics["tool_cost"] == 7
        assert 0 < metrics["cpu_ms"] < PROFILE["episode_cpu_seconds"] * 1000
        assert result["elapsed_ms"] < PROFILE["episode_wall_seconds"] * 1000 + 1000
        assert [step["tool"] for step in metrics["trajectory"]] == [
            "list_records",
            "read_record",
            "read_record",
            "read_record",
            "read_record",
            "submit_report",
        ]


@REAL
@pytest.mark.sandbox
def test_actual_public_execution_is_run_only_and_state_is_private(sandbox):
    public = project_public_problem(PROBLEM)
    source = REFERENCE.replace('"phase": "listed"', '"phase": "listed", "secret": "state-canary"')
    result = evaluate_public(public, source, sandbox)
    assert result["verdict"] == "accepted" and result["total"] == 1
    assert "state-canary" not in json.dumps(result)
    with pytest.raises(ValueError, match="public and hidden"):
        python_agent_tools_v1(public, source, "submit", sandbox)
    invalid = copy.deepcopy(public)
    invalid["tests"][0]["public"] = False
    with pytest.raises(ValueError):
        evaluate_public(invalid, source, sandbox)


@REAL
@pytest.mark.sandbox
def test_actual_worst_source_encoding_and_output_boundaries(sandbox):
    runtime = AgentRuntime(sandbox)
    # 20,000 control characters encode to 120,000 source bytes in JSON. Syntax
    # is invalid, but even a full state and the largest tool observation must
    # reach guest compilation, not fail at the request transport boundary.
    state = ["x" * 128] * 7 + ["x" * 96]
    validate_state(state)
    assert len(json.dumps(state).encode()) == PROFILE["state_bytes"]
    observation = {
        "task": TASK,
        "last_result": {"ids": [str(index) + "x" * 31 for index in range(4)]},
        "remaining_turns": 6,
    }
    source = "\0" * 20000
    assert (
        len(
            json.dumps({"source": source, "function": "act", "args": [observation, state]}).encode()
        )
        < PROFILE["input_bytes"]
    )
    assert runtime.execute(source, observation, state, EpisodeBudget()).verdict == "syntax_error"
    prefix = 'def act(o,s): return {"tool":"list_records","args":{},"state":None}\n#'
    source = prefix + "😀" * (20000 - len(prefix))
    assert runtime.execute(source, {}, None, EpisodeBudget()).verdict == "ok"
    overhead = len(json.dumps({"value": ""}).encode())
    assert (
        runtime.execute(
            f'def act(o,s): return "x"*{4096 - overhead}', {}, None, EpisodeBudget()
        ).verdict
        == "ok"
    )
    assert (
        runtime.execute(
            f'def act(o,s): return "x"*{4097 - overhead}', {}, None, EpisodeBudget()
        ).verdict
        == "output_limit"
    )


@REAL
@pytest.mark.sandbox
def test_actual_remaining_episode_deadline_and_cpu_budget_kill_and_reap(sandbox):
    runtime = AgentRuntime(sandbox)
    # Remaining-budget boundary setup; both terminations below execute actual
    # WASI children. These are not claims that earlier CPU/wall use was measured.
    wall = EpisodeBudget(started=time.monotonic() - PROFILE["episode_wall_seconds"] + 0.3)
    before = time.monotonic()
    result = runtime.execute("import time\ndef act(o,s): time.sleep(60)", {}, None, wall)
    assert result.verdict == "time_limit" and wall.exhaustion == "episode_wall"
    assert time.monotonic() - before < 1.5
    assert wall.children_reaped == 1 and not Path(f"/proc/{wall.last_child_pid}").exists()
    cpu = EpisodeBudget(cpu_seconds=PROFILE["episode_cpu_seconds"] - 0.03)
    result = runtime.execute("def act(o,s):\n while True: pass", {}, None, cpu)
    assert result.verdict == "time_limit" and cpu.exhaustion == "episode_cpu"
    assert cpu.children_reaped == 1 and not Path(f"/proc/{cpu.last_child_pid}").exists()
    assert cpu.cpu_seconds < PROFILE["episode_cpu_seconds"] + 0.3


@REAL
@pytest.mark.sandbox
def test_actual_cancel_between_turns_stops_without_leaving_children(sandbox):
    calls = 0

    class Cancelled(Exception):
        pass

    def callback():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Cancelled

    with pytest.raises(Cancelled):
        evaluate_public(project_public_problem(PROBLEM), REFERENCE, sandbox, callback)
    assert calls == 2


@REAL
@pytest.mark.sandbox
def test_actual_parent_death_terminates_agent_driver(tmp_path):
    helper = r"""
import os,sys
from pathlib import Path
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
import django
django.setup()
from judge.backends import configured_sandbox
from judge.agent_runtime import AgentRuntime, EpisodeBudget
import judge.agent_runtime as agent
real = agent.subprocess.Popen
def tracked(*args, **kwargs):
    child = real(*args, **kwargs)
    Path(sys.argv[1]).write_text(str(child.pid))
    return child
agent.subprocess.Popen = tracked
sandbox = configured_sandbox()
sandbox.preflight()
AgentRuntime(sandbox).execute("import time\ndef act(o,s): time.sleep(60)", {}, None, EpisodeBudget())
"""
    marker = tmp_path / "agent-child"
    parent = subprocess.Popen(
        [sys.executable, "-B", "-c", helper, str(marker)], stderr=subprocess.PIPE
    )
    pidfd = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert parent.poll() is None, parent.stderr.read().decode()
            time.sleep(0.02)
        assert marker.exists()
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        pidfd = libc.syscall(434, int(marker.read_text()), 0)
        assert pidfd >= 0
        assert not select.select([pidfd], [], [], 0.2)[0]
        parent.kill()
        parent.wait(timeout=3)
        assert select.select([pidfd], [], [], 3)[0], "Agent driver survived supervisor death"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        parent.stderr.close()
        if pidfd is not None:
            if not select.select([pidfd], [], [], 0)[0]:
                assert libc.syscall(424, pidfd, signal.SIGKILL, 0, 0) == 0
            os.close(pidfd)
