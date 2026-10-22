"""Python complete-program JSON I/O contract; no host execution of learner code."""

import json
import math

from judge.wasi_policy import OUTPUT

# This first guest compiles only: learner source is never evaluated here, so
# its guest exit status cannot be forged by a learner os._exit().
COMPILE_GUEST = r"""
import json, sys
request = json.load(sys.stdin)
try:
    compile(request["source"], "solution.py", "exec")
except SyntaxError:
    sys.exit(3)
except MemoryError:
    sys.exit(4)
sys.stdout.write('{"value":null}')
"""

# The driver supplies actual guest fd0, and passes source only in WASI argv
# (never host process argv). Both text and byte/fd stdin reads work normally.
PROGRAM_GUEST = r"""
import sys
source = sys.argv[1]
sys.argv = ["solution.py"]
try:
    code = compile(source, "solution.py", "exec")
    exec(code, {"__name__": "__main__", "__file__": "solution.py"})
except SystemExit as error:
    sys.exit(0 if error.code is None or isinstance(error.code, int) and error.code == 0 else 2)
except BaseException:
    sys.exit(2)
"""


def validate_request(source, stdin):
    if not isinstance(source, str) or not isinstance(stdin, str):
        raise ValueError("Program source and stdin must be UTF-8 strings.")
    # No encoding substitution: the bytes read by the guest match the contract.
    source.encode("utf-8")
    stdin.encode("utf-8")


def parse_output(output, *, envelope=False):
    """Decode bounded guest JSON for every mode, before any value is trusted."""

    if len(output) > OUTPUT:
        raise ValueError("Guest output exceeds the byte limit.")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON output key.")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("Non-finite JSON output.")

    try:
        value = json.loads(output.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("JSON output exceeds decoder limits.") from exc
    if envelope:
        if not isinstance(value, dict) or set(value) != {"value"}:
            raise ValueError("Invalid function result envelope.")
        value = value["value"]
    nodes = 0

    def validate(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 20000:
            raise ValueError("JSON output exceeds structural limits.")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON output.")
        if isinstance(item, dict):
            for child in item.values():
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)

    validate(value, 0)
    return value
