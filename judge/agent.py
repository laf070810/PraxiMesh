"""Host-owned ledger tools, state transitions, success checks and trajectories."""

import json
import math
import re
import time

from judge.agent_policy import PROFILE, profile_digest
from judge.agent_runtime import AgentRuntime, EpisodeBudget
from judge.results import LABELS

EVALUATOR = "python-agent-tools-v1"
ASSESSMENT = {"kind": "approved-ledger-v1", "resource_profile": "wasi-agent-v1"}
PUBLIC_KEYS = {"evaluator", "language", "function", "assessment", "tests"}
CASE_KEYS = {"name", "public", "environment"}
RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
TASK = "List all records, read each record, then submit the count and total amount of approved records. Amounts are integer minor units."


class ProtocolError(ValueError):
    """Fixed non-sensitive failure codes, never learner-controlled text."""


def _keys(value, expected):
    return type(value) is dict and set(value) == expected


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def validate_cases(version, *, public_only=False):
    if (
        type(version) is not dict
        or version.get("evaluator") != EVALUATOR
        or version.get("language") != "python"
        or version.get("function") != "act"
        or version.get("assessment") != ASSESSMENT
    ):
        raise ValueError("Unsupported agent assessment or resource profile.")
    tests = version.get("tests")
    if type(tests) is not list or not 1 <= len(tests) <= PROFILE["max_episodes"]:
        raise ValueError("Agent assessment requires 1 to 8 episodes.")
    names = set()
    visibility = set()
    for case in tests:
        if not _keys(case, CASE_KEYS):
            raise ValueError("Invalid agent episode fields.")
        if (
            type(case["name"]) is not str
            or not 1 <= len(case["name"]) <= 80
            or case["name"] in names
            or type(case["public"]) is not bool
            or not _keys(case["environment"], {"records"})
        ):
            raise ValueError("Invalid agent episode metadata.")
        names.add(case["name"])
        visibility.add(case["public"])
        records = case["environment"]["records"]
        if type(records) is not list or not 1 <= len(records) <= 4:
            raise ValueError("Each agent episode requires 1 to 4 records.")
        ids = set()
        for record in records:
            if (
                not _keys(record, {"id", "amount", "approved"})
                or type(record["id"]) is not str
                or not RECORD_ID.fullmatch(record["id"])
                or record["id"] in ids
                or not _integer(record["amount"], -100000, 100000)
                or type(record["approved"]) is not bool
            ):
                raise ValueError("Invalid agent ledger record.")
            ids.add(record["id"])
    if public_only:
        if visibility != {True}:
            raise ValueError("Public agent execution only accepts public episodes.")
    elif visibility != {False, True}:
        raise ValueError("Formal agent problems require public and hidden episodes.")


def validate_public_problem(value):
    if not _keys(value, PUBLIC_KEYS):
        raise ValueError("Public agent payload has unexpected fields.")
    validate_cases(value, public_only=True)
    return value


def project_public_problem(version):
    validate_cases(version)
    result = {key: version[key] for key in PUBLIC_KEYS if key != "tests"}
    result["tests"] = [case for case in version["tests"] if case["public"]]
    # No caller-owned mutable object is exposed by the projection.
    result = json.loads(json.dumps(result, allow_nan=False))
    return validate_public_problem(result)


def validate_state(state):
    nodes = 0

    def walk(value, depth):
        nonlocal nodes
        nodes += 1
        if depth > PROFILE["state_depth"] or nodes > PROFILE["state_nodes"]:
            raise ProtocolError("invalid_state")
        if value is None or type(value) is bool:
            return
        if type(value) is int:
            if abs(value) > 1_000_000_000:
                raise ProtocolError("invalid_state")
        elif type(value) is float:
            if not math.isfinite(value) or abs(value) > 1_000_000_000:
                raise ProtocolError("invalid_state")
        elif type(value) is str:
            if len(value) > 128:
                raise ProtocolError("invalid_state")
        elif type(value) is list:
            for item in value:
                walk(item, depth + 1)
        elif type(value) is dict:
            if any(type(key) is not str or len(key) > 32 for key in value):
                raise ProtocolError("invalid_state")
            for item in value.values():
                walk(item, depth + 1)
        else:
            raise ProtocolError("invalid_state")

    walk(state, 0)
    try:
        wire = json.dumps(state, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid_state") from exc
    if len(wire) > PROFILE["state_bytes"]:
        raise ProtocolError("invalid_state")


class Ledger:
    def __init__(self, records):
        self.records = {record["id"]: dict(record) for record in records}
        self.listed = False
        self.read_ids = set()
        self.report = None
        self.cost = 0
        self.trajectory = []

    def apply(self, action, turn):
        if not _keys(action, {"tool", "args", "state"}):
            raise ProtocolError("invalid_action")
        validate_state(action["state"])
        tool, args = action["tool"], action["args"]
        if type(tool) is not str or tool not in {"list_records", "read_record", "submit_report"}:
            raise ProtocolError("unauthorized_tool")
        cost = 2 if tool == "submit_report" else 1
        if self.cost + cost > PROFILE["max_tool_cost"]:
            raise ProtocolError("tool_budget")
        complete = False
        if tool == "list_records":
            if not _keys(args, set()):
                raise ProtocolError("invalid_arguments")
            self.listed = True
            result = {"ids": list(self.records)}
        elif tool == "read_record":
            if not _keys(args, {"id"}) or type(args["id"]) is not str:
                raise ProtocolError("invalid_arguments")
            if not self.listed or args["id"] not in self.records:
                raise ProtocolError("unauthorized_record")
            self.read_ids.add(args["id"])
            result = dict(self.records[args["id"]])
        else:
            if not _keys(args, {"total", "count"}) or not (
                _integer(args["total"], -400000, 400000) and _integer(args["count"], 0, 4)
            ):
                raise ProtocolError("invalid_arguments")
            if not self.listed or self.read_ids != set(self.records):
                raise ProtocolError("incomplete_observation")
            expected = {
                "total": sum(r["amount"] for r in self.records.values() if r["approved"]),
                "count": sum(r["approved"] for r in self.records.values()),
            }
            # Actual environment mutation. The final check uses this host-owned
            # report; a learner's state/success/trajectory never substitutes for it.
            self.report = dict(args)
            complete = self.report == expected
            result = {"report_saved": True, "matches": complete}
        self.cost += cost
        self.trajectory.append(
            {"turn": turn, "tool": tool, "args": dict(args), "result": result, "cost": cost}
        )
        return result, complete, tool == "submit_report"


def run_episode(records, source, runtime, on_case):
    ledger = Ledger(records)
    budget = EpisodeBudget()
    state = None
    last_result = None
    outcome = "wrong_answer"
    reason = "turn_budget"
    for _ in range(PROFILE["max_turns"]):
        on_case()
        observation = {
            "task": TASK,
            "last_result": last_result,
            "remaining_turns": PROFILE["max_turns"] - budget.turns,
        }
        execution = runtime.execute(source, observation, state, budget)
        if execution.verdict != "ok":
            outcome = execution.verdict
            reason = budget.exhaustion or execution.verdict
            break
        try:
            last_result, complete, finished = ledger.apply(execution.value, budget.turns)
        except ProtocolError as exc:
            outcome, reason = "wrong_answer", str(exc)
            break
        state = execution.value["state"]
        if finished:
            outcome = "accepted" if complete else "wrong_answer"
            reason = "completed" if complete else "incorrect_report"
            break
    return {
        "verdict": outcome,
        "reason": reason,
        "elapsed_ms": int((time.monotonic() - budget.started) * 1000),
        "metrics": {
            "resource_profile": PROFILE["id"],
            "profile_digest": profile_digest(),
            "turns": budget.turns,
            "tool_cost": ledger.cost,
            "cpu_ms": round(budget.cpu_seconds * 1000, 3),
            "fuel_reserved": budget.fuel_reserved,
            "output_reserved": budget.output_reserved,
            "trajectory": ledger.trajectory,
        },
    }


def _evaluate(version, source, mode, sandbox, on_case):
    if type(source) is not str or len(source) > 20000:
        raise ValueError("Agent source must be at most 20000 characters.")
    runtime = AgentRuntime(sandbox)
    cases = [case for case in version["tests"] if mode == "submit" or case["public"]]
    passed = elapsed = 0
    feedback = []
    verdict = "accepted"
    for case in cases:
        result = run_episode(case["environment"]["records"], source, runtime, on_case)
        elapsed += result["elapsed_ms"]
        outcome = result["verdict"]
        if case["public"]:
            feedback.append(
                {
                    "name": case["name"],
                    "verdict": outcome,
                    "label": LABELS[outcome],
                    "reason": result["reason"],
                    "metrics": result["metrics"],
                }
            )
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
    }


def python_agent_tools_v1(version, source, mode, sandbox, on_case=lambda: None):
    if mode not in {"run", "submit"}:
        raise ValueError("Unsupported evaluation mode.")
    validate_cases(version)
    return _evaluate(version, source, mode, sandbox, on_case)


def evaluate_public(version, source, sandbox, on_case=lambda: None):
    validate_public_problem(version)
    return _evaluate(version, source, "run", sandbox, on_case)
