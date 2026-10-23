"""Bounded verification of public Agent traces against the owned problem version.

Only the trusted worker writes these records. Replaying public tool transitions
checks their consistency; it is not proof for client-uploaded execution claims.
"""

import copy
import json
import math

from judge.agent import Ledger, ProtocolError
from judge.agent_policy import PROFILE, profile_digest
from judge.results import LABELS

REASONS = {
    "accepted": {"completed"},
    "wrong_answer": {
        "turn_budget",
        "invalid_action",
        "invalid_state",
        "unauthorized_tool",
        "tool_budget",
        "invalid_arguments",
        "unauthorized_record",
        "incomplete_observation",
        "incorrect_report",
    },
    "time_limit": {"time_limit", "episode_wall", "episode_cpu", "turn_wall"},
    "runtime_error": {"runtime_error"},
    "syntax_error": {"syntax_error"},
    "memory_limit": {"memory_limit"},
    "output_limit": {"output_limit"},
}


def _exact(value, keys):
    return type(value) is dict and set(value) == set(keys)


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def public_feedback(version, result, mode):
    invalid = ValueError("Invalid Agent result evidence")
    tests = version.data.get("tests")
    if mode not in {"submit", "run"} or type(tests) is not list or not 2 <= len(tests) <= 8:
        raise invalid
    selected = [case for case in tests if mode == "submit" or case["public"]]
    if not _exact(result, {"verdict", "label", "passed", "total", "elapsed_ms", "public_cases"}):
        raise invalid
    passed, total, verdict = result["passed"], result["total"], result["verdict"]
    if (
        not _integer(total, 1, 8)
        or total != len(selected)
        or not _integer(passed, 0, total)
        or verdict not in REASONS
        or (verdict == "accepted") != (passed == total)
        or result["label"] != LABELS[verdict]
        or not _integer(result["elapsed_ms"], 0, 10**9)
    ):
        raise invalid
    reached = selected[: passed + (verdict != "accepted")]
    public = [(index, case) for index, case in enumerate(reached) if case["public"]]
    feedback = result["public_cases"]
    if type(feedback) is not list or len(feedback) != len(public):
        raise invalid
    for row, (index, case) in zip(feedback, public, strict=True):
        expected = "accepted" if index < passed else verdict
        if (
            not _exact(row, {"name", "verdict", "label", "reason", "metrics"})
            or row["name"] != case["name"]
            or row["verdict"] != expected
            or row["label"] != LABELS[expected]
            or row["reason"] not in REASONS[expected]
        ):
            raise invalid
        metrics = row["metrics"]
        if (
            not _exact(
                metrics,
                {
                    "resource_profile",
                    "profile_digest",
                    "turns",
                    "tool_cost",
                    "cpu_ms",
                    "fuel_reserved",
                    "output_reserved",
                    "trajectory",
                },
            )
            or metrics["resource_profile"] != PROFILE["id"]
            or metrics["profile_digest"] != profile_digest()
            or not _integer(metrics["turns"], 0, 6)
            or not _integer(metrics["tool_cost"], 0, 7)
            or type(metrics["cpu_ms"]) not in {int, float}
            or not 0 <= metrics["cpu_ms"] <= 10**9
            or not math.isfinite(metrics["cpu_ms"])
            or type(metrics["fuel_reserved"]) is not int
            or metrics["fuel_reserved"] != metrics["turns"] * PROFILE["turn_fuel"]
            or type(metrics["output_reserved"]) is not int
            or metrics["output_reserved"] != metrics["turns"] * PROFILE["turn_output_bytes"]
            or type(metrics["trajectory"]) is not list
            or not len(metrics["trajectory"]) <= metrics["turns"]
        ):
            raise invalid
        ledger = Ledger(case["environment"]["records"])
        finished = complete = False
        for turn, entry in enumerate(metrics["trajectory"], 1):
            if (
                finished
                or not _exact(entry, {"turn", "tool", "args", "result", "cost"})
                or type(entry["turn"]) is not int
                or entry["turn"] != turn
            ):
                raise invalid
            try:
                _, complete, finished = ledger.apply(
                    {"tool": entry["tool"], "args": entry["args"], "state": None}, turn
                )
            except ProtocolError:
                raise invalid from None
            # JSON equality preserves boolean/numeric distinction and rejects
            # unexpected nested data, including raw learner state or hidden rows.
            if json.dumps(entry, sort_keys=True, allow_nan=False) != json.dumps(
                ledger.trajectory[-1], sort_keys=True, allow_nan=False
            ):
                raise invalid
        if ledger.cost != metrics["tool_cost"]:
            raise invalid
        if expected == "accepted" and not (
            complete and finished and len(ledger.trajectory) == metrics["turns"]
        ):
            raise invalid
        if expected != "accepted" and complete:
            raise invalid
        if finished and (row["reason"] != ("completed" if complete else "incorrect_report")):
            raise invalid
        if row["reason"] == "incorrect_report" and not finished:
            raise invalid
    return copy.deepcopy(result)
