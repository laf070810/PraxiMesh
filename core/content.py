"""Validation never executes submitted/reference code or loads executable plugins."""

import copy
import hashlib
import json
import math
from pathlib import Path

from django.conf import settings
from django.db import transaction
from jsonschema import Draft202012Validator

from core.models import KnowledgeNode, Problem, ProblemVersion


class ContentError(ValueError):
    pass


def bounded_json_shape(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 20000:
            raise ContentError("Test JSON exceeds the runtime structural limits.")
        children = (
            item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        )
        pending.extend((child, depth + 1) for child in children)


def javascript_values(value, depth=0):
    """Content must fit the same finite, safe JSON contract as the guest bridge."""
    if depth > 32:
        raise ContentError("JavaScript values exceed the supported nesting depth.")
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or (value == int(value) and abs(value) > 2**53 - 1):
            raise ContentError("JavaScript numbers must be finite and integers must be safe.")
    elif isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, list):
        for item in value:
            javascript_values(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContentError("JavaScript object keys must be strings.")
            key.encode("utf-8")
            javascript_values(item, depth + 1)
    else:
        raise ContentError("Unsupported JavaScript JSON value.")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def load_package(path):
    def invalid_constant(value):
        raise ContentError(f"Nonfinite JSON constant: {value}")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContentError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ContentError("Content package exceeds 5 MB.")
    data = json.loads(
        path.read_text(), parse_constant=invalid_constant, object_pairs_hook=unique_keys
    )
    validate_package(data)
    return data


def validate_package(data):
    schema = json.loads((settings.CONTENT_ROOT / "schema.json").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        raise ContentError("; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8]))
    # Also catches nonfinite numbers when called directly rather than via load_package.
    try:
        digest(data)
    except (ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ContentError("Nonfinite numeric values are not allowed.") from exc
    nodes = {n["id"]: n for n in data["nodes"]}
    problems = {p["id"]: p for p in data["problems"]}
    if len(nodes) != len(data["nodes"]) or len(problems) != len(data["problems"]):
        raise ContentError("Duplicate stable IDs.")
    if nodes.keys() & problems.keys():
        raise ContentError("Node and problem IDs must be globally unique.")

    def reference(target, kind):
        if target not in nodes or nodes[target]["kind"] != kind:
            raise ContentError(f"Broken {kind} reference: {target}")

    aliases = {}
    required = {n: [] for n in nodes}
    for node in nodes.values():
        for field, kind in [("domains", "domain"), ("topics", "topic"), ("concepts", "concept")]:
            for target in node[field]:
                reference(target, kind)
        if node["kind"] == "concept":
            if not node["domains"] or not node["outcomes"] or not node["body"]:
                raise ContentError(f"Concept needs domains, outcomes and explanation: {node['id']}")
            for alias in [node["title"], *node["aliases"]]:
                key = alias.casefold().strip()
                if key in aliases and aliases[key] != node["id"]:
                    raise ContentError(f"Ambiguous concept alias: {alias}")
                aliases[key] = node["id"]
        if node["kind"] == "tutorial" and (not node["body"] or not node["concepts"]):
            raise ContentError("Tutorial requires text and linked concepts.")
        if node["kind"] == "topic" and not node["domains"]:
            raise ContentError("Topic requires at least one domain.")
        if node["relations"] and node["kind"] != "concept":
            raise ContentError("Only concepts have prerequisite relations.")
        seen = set()
        for edge in node["relations"]:
            reference(edge["target"], "concept")
            if edge["target"] == node["id"] or edge["target"] in seen:
                raise ContentError("Self, duplicate or contradictory concept relation.")
            seen.add(edge["target"])
            if edge["kind"] == "required":
                required[node["id"]].append(edge["target"])
        if node["kind"] == "path" and not node["steps"]:
            raise ContentError("Path must contain steps.")
        if node["kind"] != "path" and node["steps"]:
            raise ContentError("Only paths have steps.")
        seen_steps = set()
        for step in node["steps"]:
            target = step["target"]
            if target in seen_steps:
                raise ContentError("Duplicate path step.")
            seen_steps.add(target)
            if step["kind"] == "problem":
                if target not in problems:
                    raise ContentError(f"Broken problem reference: {target}")
            else:
                reference(target, "tutorial")
    # Iterative topological removal: cycles rejected without recursion depth hazards.
    pending = {key: set(value) for key, value in required.items()}
    while pending:
        roots = {key for key, value in pending.items() if not value}
        if not roots:
            raise ContentError("Required prerequisite graph contains a cycle.")
        pending = {key: value - roots for key, value in pending.items() if key not in roots}
    for problem in problems.values():
        for concept in problem["concepts"]:
            reference(concept, "concept")
        if not any(t["public"] for t in problem["tests"]) or all(
            t["public"] for t in problem["tests"]
        ):
            raise ContentError("Every problem needs public examples and protected tests.")
        names = [t["name"] for t in problem["tests"]]
        if len(names) != len(set(names)):
            raise ContentError("Duplicate test names.")
        if problem["evaluator"] in SPECIALIST_EVALUATORS:
            module = specialist_module(problem["evaluator"])
            try:
                (
                    module.validate_cases
                    if problem["evaluator"] == "python-agent-tools-v1"
                    else module.validate_problem
                )(problem)
            except ValueError:
                raise ContentError("Invalid specialist data or evaluation contract.") from None
            continue
        for case in problem["tests"]:
            input_value = (
                case["stdin"] if problem["evaluator"] == "python-stdio-v1" else case["args"]
            )
            if (
                len(json.dumps(input_value, ensure_ascii=False).encode()) > 100000
                or len(json.dumps(case["expected"]).encode()) > 32000
            ):
                raise ContentError("Test exceeds input/output budget.")
            bounded_json_shape(case["expected"])
            if problem["language"] == "javascript":
                try:
                    bounded_json_shape(case["args"])
                    javascript_values(case["args"])
                    javascript_values(case["expected"])
                except (UnicodeError, OverflowError) as exc:
                    raise ContentError("Unsupported JavaScript JSON value.") from exc


@transaction.atomic
def import_package(data):
    from localization.packages import snapshot
    from organizations.publishing import official_import_scope

    validate_package(data)
    owned_nodes, owned_problems = official_import_scope(data)
    # Save existing knowledge before replacement; previous problem snapshots stay untouched.
    for previous in KnowledgeNode.objects.select_for_update():
        snapshot(previous)
    # Full replacement publication: absent IDs are withdrawn, never deleted.
    KnowledgeNode.objects.exclude(pk__in=owned_nodes).update(published=False)
    Problem.objects.exclude(pk__in=owned_problems).update(published=False)
    for node in data["nodes"]:
        stored, _ = KnowledgeNode.objects.update_or_create(
            id=node["id"],
            defaults={
                "kind": node["kind"],
                "title": node["title"],
                "data": node,
                "published": True,
            },
        )
        snapshot(stored)
    for item in data["problems"]:
        problem, _ = Problem.objects.get_or_create(id=item["id"])
        version, created = ProblemVersion.objects.get_or_create(
            problem=problem,
            revision=item["revision"],
            defaults={"digest": digest(item), "data": item},
        )
        if not created and version.digest != digest(item):
            raise ContentError(f"Immutable revision changed: {item['id']} v{item['revision']}")
        if problem.current_id and problem.current.revision > version.revision:
            raise ContentError("Cannot silently roll back a published revision.")
        problem.current = version
        problem.published = True
        problem.save()


SPECIALIST_EVALUATORS = frozenset(
    {"python-ml-linear-v1", "python-weighted-fit-v1", "python-agent-tools-v1"}
)


def specialist_module(evaluator):
    from judge import agent, ml, scientific

    return {
        "python-agent-tools-v1": agent,
        "python-ml-linear-v1": ml,
        "python-weighted-fit-v1": scientific,
    }[evaluator]


def public_execution(version):
    """Explicit public execution data; references and hidden cases never enter it."""
    source = version.data
    evaluator = source["evaluator"]
    if evaluator in SPECIALIST_EVALUATORS:
        return specialist_module(evaluator).project_public_problem(source)
    stdio = evaluator == "python-stdio-v1"
    fields = ("name", "public", "stdin" if stdio else "args", "expected")
    return {
        "evaluator": evaluator,
        "language": source["language"],
        **(
            {"output_format": source["output_format"]}
            if stdio
            else {"function": source["function"]}
        ),
        "comparison": copy.deepcopy(source["comparison"]),
        "tests": [
            {key: copy.deepcopy(case[key]) for key in fields}
            for case in source["tests"]
            if case["public"]
        ],
    }


def public_problem(version):
    # An explicit allowlist prevents newly added private fields from leaking.
    fields = (
        "id",
        "revision",
        "title",
        "summary",
        "statement",
        "constraints",
        "outcomes",
        "concepts",
        "difficulty",
        "language",
        "evaluator",
        "starter",
        "hints",
        "comparison",
        "changelog",
        "tags",
    )
    data = {key: copy.deepcopy(version.data[key]) for key in fields}
    execution = public_execution(version)
    if version.data["evaluator"] in SPECIALIST_EVALUATORS:
        data.update({key: value for key, value in execution.items() if key != "tests"})
        data["examples"] = execution["tests"]
        return data
    stdio = version.data["evaluator"] == "python-stdio-v1"
    if stdio:
        data["output_format"] = version.data["output_format"]
    else:
        data["function"] = version.data["function"]
    data["examples"] = [
        {
            **case,
            **(
                {}
                if stdio
                else {
                    "call": f"{data['function']}({', '.join(json.dumps(arg, ensure_ascii=False) if data['language'] == 'javascript' else repr(arg) for arg in case['args'])})"
                }
            ),
        }
        for case in execution["tests"]
    ]
    return data
