"""Fixed real-WASI agent probes; never execute these sources in host Python."""

from judge.agent import ASSESSMENT

REFERENCE = """def act(observation, state):
    if state is None:
        return {"tool": "list_records", "args": {}, "state": {"phase": "listed"}}
    result = observation["last_result"]
    if state["phase"] == "listed":
        ids = result["ids"]
        return {"tool": "read_record", "args": {"id": ids[0]}, "state": {"phase": "reading", "ids": ids, "next": 1, "total": 0, "count": 0}}
    if result["approved"]:
        state["total"] += result["amount"]
        state["count"] += 1
    if state["next"] < len(state["ids"]):
        record_id = state["ids"][state["next"]]
        state["next"] += 1
        return {"tool": "read_record", "args": {"id": record_id}, "state": state}
    return {"tool": "submit_report", "args": {"total": state["total"], "count": state["count"]}, "state": None}
"""

PROBLEM = {
    "evaluator": "python-agent-tools-v1",
    "language": "python",
    "function": "act",
    "assessment": ASSESSMENT,
    "tests": [
        {
            "name": "public-four-records",
            "public": True,
            "environment": {
                "records": [
                    {"id": "receipt-a", "amount": 125, "approved": True},
                    {"id": "receipt-b", "amount": -20, "approved": True},
                    {"id": "receipt-c", "amount": 999, "approved": False},
                    {"id": "receipt-d", "amount": 0, "approved": True},
                ]
            },
        },
        {
            "name": "hidden-episode-canary",
            "public": False,
            "environment": {
                "records": [{"id": "hidden-record-canary", "amount": -7, "approved": False}]
            },
        },
    ],
}

PROBES = [
    ("correct", REFERENCE, "accepted"),
    (
        "guessed-success-without-observation",
        'def act(o,s): return {"tool":"submit_report","args":{"total":105,"count":3},"state":None}',
        "wrong_answer",
    ),
    (
        "unauthorized-tool",
        'def act(o,s): return {"tool":"shell","args":{"cmd":"true"},"state":None}',
        "wrong_answer",
    ),
    (
        "unauthorized-record",
        'def act(o,s):\n if s is None: return {"tool":"list_records","args":{},"state":1}\n return {"tool":"read_record","args":{"id":"other-user-record"},"state":None}',
        "wrong_answer",
    ),
    (
        "state-is-not-authorization",
        'def act(o,s): return {"tool":"submit_report","args":{"total":105,"count":3},"state":{"listed":True,"read_ids":["receipt-a","receipt-b","receipt-c","receipt-d"]}}',
        "wrong_answer",
    ),
    (
        "forged-trajectory",
        'def act(o,s): return {"tool":"list_records","args":{},"state":None,"trajectory":[{"success":True}]}',
        "wrong_answer",
    ),
    (
        "loop-turn-budget",
        'def act(o,s): return {"tool":"list_records","args":{},"state":None}',
        "wrong_answer",
    ),
    ("incorrect-report", REFERENCE.replace('if result["approved"]:', "if True:"), "wrong_answer"),
    (
        "bool-count-is-not-integer",
        REFERENCE.replace('"count": state["count"]', '"count": True'),
        "wrong_answer",
    ),
    (
        "state-limit",
        'def act(o,s): return {"tool":"list_records","args":{},"state":"x"*129}',
        "wrong_answer",
    ),
    ("fuel", "def act(o,s):\n while True: pass", "time_limit"),
    ("wall", "import time\ndef act(o,s): time.sleep(60)", "time_limit"),
    ("output", 'def act(o,s): print("x"*10000)', "output_limit"),
    ("memory", "def act(o,s): return bytearray(512*1024*1024)", "memory_limit"),
    ("syntax", "def act(: pass", "syntax_error"),
    ("exception", "def act(o,s): return 1/0", "runtime_error"),
]
