"""Small real class/interface probes, separate from the published exercise catalogue."""

REFERENCE = """class Counter:
    def __init__(self, initial):
        self.value = initial
    def add(self, amount):
        self.value += amount
        return self.value
    def read(self):
        if self.value < 0:
            raise IndexError()
        return self.value
"""

PROBLEM = {
    "evaluator": "python-object-api-v1",
    "language": "python",
    "interface": {
        "class_name": "Counter",
        "constructor_arity": 1,
        "methods": {
            "add": {"arity": 1, "raises": []},
            "read": {"arity": 0, "raises": ["IndexError"]},
        },
    },
    "comparison": {"kind": "exact", "absolute": 0, "relative": 0},
    "tests": [
        {
            "name": "public state transitions",
            "public": True,
            "operations": [
                {"new": "a", "args": [1]},
                {"call": "a", "method": "add", "args": [2]},
                {"call": "a", "method": "read", "args": []},
            ],
            "expected": [{"value": None}, {"value": 3}, {"value": 3}],
        },
        {
            "name": "private independent objects and exception recovery",
            "public": False,
            "operations": [
                {"new": "left", "args": [10]},
                {"new": "right", "args": [-2]},
                {"call": "left", "method": "add", "args": [5]},
                {"call": "right", "method": "read", "args": []},
                {"call": "left", "method": "read", "args": []},
                {"call": "right", "method": "add", "args": [4]},
                {"call": "right", "method": "read", "args": []},
            ],
            "expected": [
                {"value": None},
                {"value": None},
                {"value": 15},
                {"raises": "IndexError"},
                {"value": 15},
                {"value": 2},
                {"value": 2},
            ],
        },
    ],
}

PROBES = [
    ("reference", REFERENCE, "accepted"),
    ("shared-class-state", REFERENCE.replace("self.value", "Counter.value"), "wrong_answer"),
    ("missing-method", "class Counter:\n def __init__(self, initial): pass", "runtime_error"),
    ("syntax", "class Counter(:", "syntax_error"),
    (
        "wrong-exception",
        REFERENCE.replace("raise IndexError()", "raise ValueError()"),
        "runtime_error",
    ),
    (
        "forged-score",
        REFERENCE.replace("return self.value", "return {'verdict': 'accepted', 'score': 100}"),
        "wrong_answer",
    ),
]
