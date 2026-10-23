"""Bounded object API contracts; learner code runs only inside the configured sandbox."""

import copy
import json
import math
import re

from judge.results import LABELS, matches

CONTRACT = "python-object-api-v1"
ENTRYPOINT = "praximesh_object_api"
CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9_]{0,59}")
NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
EXCEPTIONS = frozenset(
    {"IndexError", "KeyError", "ValueError", "TypeError", "OverflowError", "ZeroDivisionError"}
)
FAILURES = frozenset(
    {"syntax_error", "runtime_error", "memory_limit", "time_limit", "output_limit"}
)
COMPARISON = {"kind": "exact", "absolute": 0, "relative": 0}
MAX_JSON_BYTES = 24000
MAX_OPERATIONS = 64
MAX_INSTANCES = 8

# This driver is guest code, not a security boundary. No expected values or
# infrastructure credentials enter it. Host comparison treats its output as data.
DRIVER = r"""
import json as _api_json

def _api_snapshot(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 4096:
            raise ValueError("Unsupported API return shape")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise TypeError("API object keys must be strings")
            children = item.values()
        elif type(item) is list:
            children = item
        elif type(item) in (str, int, float, bool) or item is None:
            children = ()
        else:
            raise TypeError("API results must be JSON values")
        pending.extend((child, depth + 1) for child in children)
    wire = _api_json.dumps(value, ensure_ascii=False, allow_nan=False)
    return _api_json.loads(wire)

def praximesh_object_api(source, interface, operations):
    py_type, py_isinstance = type, isinstance
    py_getattr, py_callable = getattr, callable
    exceptions = {t.__name__: t for t in
        (IndexError, KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError)}
    try:
        try:
            code = compile(source, "solution.py", "exec", dont_inherit=True)
        except SyntaxError:
            return {"status": "syntax_error"}
        namespace = {"__name__": "solution"}
        exec(code, namespace)
        object_class = namespace[interface["class_name"]]
        if not py_isinstance(object_class, py_type):
            raise TypeError("The API entry point must be a class")
        instances, results = {}, []
        for operation in operations:
            if "new" in operation:
                instances[operation["new"]] = object_class(*operation["args"])
                results.append({"value": None})
                continue
            method = py_getattr(instances[operation["call"]], operation["method"])
            if not py_callable(method):
                raise TypeError("API methods must be callable")
            allowed = interface["methods"][operation["method"]]["raises"]
            try:
                value = method(*operation["args"])
            except BaseException as error:
                name = py_type(error).__name__
                if name not in allowed or py_type(error) is not exceptions[name]:
                    raise
                results.append({"raises": name})
            else:
                results.append({"value": _api_snapshot(value)})
        return {"status": "ok", "results": results}
    except MemoryError:
        return {"status": "memory_limit"}
    except BaseException:
        return {"status": "runtime_error"}
"""


class APIContractError(ValueError):
    pass


def invalid():
    return APIContractError("Invalid bounded object API contract.")


def bounded_value(value):
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 4096:
            raise invalid()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise invalid()
            children = item.values()
        elif type(item) is list:
            children = item
        elif type(item) in (int, float):
            if abs(item) > 1e100 or not math.isfinite(item):
                raise invalid()
            children = ()
        elif type(item) in (str, bool) or item is None:
            children = ()
        else:
            raise invalid()
        pending.extend((child, depth + 1) for child in children)
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_JSON_BYTES:
            raise invalid()
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        raise invalid() from None


def validate_interface(interface):
    if (
        type(interface) is not dict
        or set(interface) != {"class_name", "constructor_arity", "methods"}
        or type(interface["class_name"]) is not str
        or not CLASS_NAME.fullmatch(interface["class_name"])
        or type(interface["constructor_arity"]) is not int
        or not 0 <= interface["constructor_arity"] <= 8
        or type(interface["methods"]) is not dict
        or not 1 <= len(interface["methods"]) <= 16
    ):
        raise invalid()
    for name, method in interface["methods"].items():
        if (
            type(name) is not str
            or not NAME.fullmatch(name)
            or type(method) is not dict
            or set(method) != {"arity", "raises"}
            or type(method["arity"]) is not int
            or not 0 <= method["arity"] <= 8
            or type(method["raises"]) is not list
            or any(type(item) is not str or item not in EXCEPTIONS for item in method["raises"])
            or len(set(method["raises"])) != len(method["raises"])
        ):
            raise invalid()


def validate_operations(interface, operations):
    if type(operations) is not list or not 2 <= len(operations) <= MAX_OPERATIONS:
        raise invalid()
    bounded_value(operations)
    instances, calls = set(), 0
    for operation in operations:
        if type(operation) is not dict or type(operation.get("args")) is not list:
            raise invalid()
        if set(operation) == {"new", "args"}:
            name = operation["new"]
            if (
                type(name) is not str
                or not NAME.fullmatch(name)
                or name in instances
                or len(instances) == MAX_INSTANCES
                or len(operation["args"]) != interface["constructor_arity"]
            ):
                raise invalid()
            instances.add(name)
        elif set(operation) == {"call", "method", "args"}:
            if (
                type(operation["call"]) is not str
                or operation["call"] not in instances
                or type(operation["method"]) is not str
                or operation["method"] not in interface["methods"]
                or len(operation["args"]) != interface["methods"][operation["method"]]["arity"]
            ):
                raise invalid()
            calls += 1
        else:
            raise invalid()
    if not calls:
        raise invalid()


def validate_request(source, interface, operations):
    if type(source) is not str or len(source) > 20000:
        raise invalid()
    source.encode("utf-8")
    validate_interface(interface)
    validate_operations(interface, operations)


def validate_problem(problem, *, public_only=False):
    if (
        type(problem) is not dict
        or problem.get("evaluator") != CONTRACT
        or problem.get("language") != "python"
        or any(
            key in problem
            for key in (
                "function",
                "output_format",
                "assessment",
                "model_kind",
                "uncertainty_model",
            )
        )
        or problem.get("comparison") != COMPARISON
        or any(
            type(problem["comparison"][key]) not in (int, float) for key in ("absolute", "relative")
        )
    ):
        raise invalid()
    interface = problem.get("interface")
    validate_interface(interface)
    cases = problem.get("tests")
    if type(cases) is not list or not 1 <= len(cases) <= 20:
        raise invalid()
    names, visibility = set(), set()
    for case in cases:
        if (
            type(case) is not dict
            or set(case) != {"name", "public", "operations", "expected"}
            or type(case["name"]) is not str
            or not 1 <= len(case["name"]) <= 200
            or case["name"] in names
            or type(case["public"]) is not bool
            or type(case["operations"]) is not list
            or type(case["expected"]) is not list
            or len(case["expected"]) != len(case["operations"])
        ):
            raise invalid()
        names.add(case["name"])
        visibility.add(case["public"])
        validate_operations(interface, case["operations"])
        bounded_value(case["expected"])
        for operation, expected in zip(case["operations"], case["expected"], strict=True):
            if type(expected) is not dict:
                raise invalid()
            if "new" in operation:
                if expected != {"value": None}:
                    raise invalid()
            elif set(expected) == {"raises"}:
                if expected["raises"] not in interface["methods"][operation["method"]]["raises"]:
                    raise invalid()
            elif set(expected) != {"value"}:
                raise invalid()
    if visibility != ({True} if public_only else {True, False}):
        raise invalid()


def project_public_problem(problem):
    validate_problem(problem)
    return {
        key: copy.deepcopy(problem[key])
        for key in ("evaluator", "language", "interface", "comparison")
    } | {"tests": [copy.deepcopy(case) for case in problem["tests"] if case["public"]]}


def display_operations(interface, operations):
    return "\n".join(
        f"{op['new']} = {interface['class_name']}({', '.join(map(repr, op['args']))})"
        if "new" in op
        else f"{op['call']}.{op['method']}({', '.join(map(repr, op['args']))})"
        for op in operations
    )


def python_object_api_v1(version, source, mode, sandbox, on_case=lambda: None):
    validate_problem(version)
    return _evaluate(version, source, mode, sandbox, on_case)


def evaluate_public(problem, source, sandbox):
    validate_problem(problem, public_only=True)
    return _evaluate(problem, source, "run", sandbox, lambda: None)


def _evaluate(version, source, mode, sandbox, on_case):
    if mode not in {"run", "submit"} or type(source) is not str or len(source) > 20000:
        raise invalid()
    cases = [case for case in version["tests"] if mode == "submit" or case["public"]]
    passed, elapsed, feedback, verdict = 0, 0, [], "accepted"
    for case in cases:
        on_case()
        execution = sandbox.execute_api(source, version["interface"], case["operations"])
        if type(execution.elapsed_ms) is not int or execution.elapsed_ms < 0:
            raise APIContractError("Invalid sandbox result metadata.")
        elapsed += execution.elapsed_ms
        outcome = execution.verdict
        if outcome == "ok":
            data = execution.value
            outcome = "wrong_answer"
            if type(data) is dict:
                if set(data) == {"status", "results"} and data["status"] == "ok":
                    if matches(data["results"], case["expected"], COMPARISON):
                        outcome = "accepted"
                elif (
                    set(data) == {"status"}
                    and type(data["status"]) is str
                    and data["status"] in {"syntax_error", "runtime_error", "memory_limit"}
                ):
                    outcome = data["status"]
        elif outcome not in FAILURES:
            raise APIContractError("Unavailable or invalid sandbox execution result.")
        if case["public"]:
            feedback.append({"name": case["name"], "verdict": outcome, "label": LABELS[outcome]})
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
