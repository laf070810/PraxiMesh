"""Validation never executes submitted/reference code or loads executable plugins."""

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.db import transaction
from jsonschema import Draft202012Validator

from core.models import KnowledgeNode, Problem, ProblemVersion


class ContentError(ValueError):
    pass


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
    except ValueError as exc:
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
        for case in problem["tests"]:
            if (
                len(json.dumps(case["args"], ensure_ascii=False).encode()) > 100000
                or len(json.dumps(case["expected"]).encode()) > 32000
            ):
                raise ContentError("Test exceeds input/output budget.")


@transaction.atomic
def import_package(data):
    validate_package(data)
    # Full replacement publication: absent IDs are withdrawn, never deleted.
    KnowledgeNode.objects.update(published=False)
    Problem.objects.update(published=False)
    for node in data["nodes"]:
        KnowledgeNode.objects.update_or_create(
            id=node["id"],
            defaults={
                "kind": node["kind"],
                "title": node["title"],
                "data": node,
                "published": True,
            },
        )
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
        "function",
        "starter",
        "hints",
        "comparison",
        "changelog",
        "tags",
    )
    data = {key: version.data[key] for key in fields}
    data["examples"] = [
        {
            **case,
            "call": f"{data['function']}({', '.join(repr(arg) for arg in case['args'])})",
        }
        for case in version.data["tests"]
        if case["public"]
    ]
    return data
