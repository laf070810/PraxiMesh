import copy
import json
from pathlib import Path

import pytest

from core.content import (
    ContentError,
    import_package,
    load_package,
    public_problem,
    validate_package,
)
from core.models import KnowledgeNode, Problem, ProblemVersion


def test_starter_contract_and_cross_domain(package):
    validate_package(package)
    assert len(package["problems"]) == 18
    vectors = next(n for n in package["nodes"] if n["id"] == "vectors")
    assert set(vectors["domains"]) == {"numerical", "machine-learning"}
    paths = {n["id"]: n for n in package["nodes"] if n["kind"] == "path"}
    assert set(paths) == {"python-to-numerical", "sequence-to-graph", "statistics-to-models"}
    exercises = {
        key: [step["target"] for step in path["steps"] if step["kind"] == "problem"]
        for key, path in paths.items()
    }
    assert exercises["python-to-numerical"] == [
        "sum-positive",
        "unique-stable",
        "word-count",
        "lower-bound",
        "dot-product",
        "transpose",
        "normalize-rows",
        "stable-softmax",
    ]
    assert len(exercises["sequence-to-graph"]) == len(exercises["statistics-to-models"]) == 5
    assert {p["id"] for p in package["problems"]} == {
        exercise for steps in exercises.values() for exercise in steps
    }


def test_every_problem_has_distinct_targeted_mistakes(package):
    # This checks review inputs only; real rejection is tested exclusively in the sandbox suite.
    mistakes = json.loads(Path(__file__).with_name("incorrect_solutions.json").read_text())
    assert set(mistakes) == {problem["id"] for problem in package["problems"]}
    for variants in mistakes.values():
        assert len(variants) >= 2
        assert len({variant["source"] for variant in variants}) == len(variants)
        assert all(variant["mistake"].strip() for variant in variants)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "cycle",
        "alias",
        "duplicate",
        "public_only",
        "unknown_evaluator",
        "bad_step",
        "contradiction",
    ],
)
def test_invalid_content_rejected(package, mutation):
    data = copy.deepcopy(package)
    concepts = [n for n in data["nodes"] if n["kind"] == "concept"]
    if mutation == "missing":
        data["problems"][0]["concepts"] = ["missing"]
    elif mutation == "cycle":
        concepts[0]["relations"] = [{"kind": "required", "target": concepts[1]["id"]}]
    elif mutation == "alias":
        concepts[1]["aliases"].append(concepts[0]["title"])
    elif mutation == "duplicate":
        data["nodes"].append(copy.deepcopy(data["nodes"][0]))
    elif mutation == "public_only":
        for case in data["problems"][0]["tests"]:
            case["public"] = True
    elif mutation == "unknown_evaluator":
        data["problems"][0]["evaluator"] = "shell"
    elif mutation == "bad_step":
        path = next(node for node in data["nodes"] if node["id"] == "python-to-numerical")
        path["steps"][0]["target"] = "functions"
    elif mutation == "contradiction":
        concepts[1]["relations"].append({"kind": "recommended", "target": "functions"})
    with pytest.raises(ContentError):
        validate_package(data)


def test_duplicate_json_keys_and_nonfinite_are_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}')
    with pytest.raises(ContentError, match="Duplicate"):
        load_package(path)
    path.write_text('{"value": NaN}')
    with pytest.raises(ContentError, match="Nonfinite"):
        load_package(path)


def test_immutable_revision_import_is_atomic(catalog):
    original = ProblemVersion.objects.get(problem_id="sum-positive")
    changed = copy.deepcopy(catalog)
    changed["problems"][0]["statement"] = "Unauthorized revision replacement"
    changed["nodes"][0]["title"] = "Should roll back"
    with pytest.raises(ContentError, match="Immutable"):
        import_package(changed)
    original.refresh_from_db()
    assert "Unauthorized" not in original.data["statement"]
    assert KnowledgeNode.objects.get(id="programming").title == "编程语言与基础"
    assert Problem.objects.filter(published=True).count() == len(catalog["problems"])


def test_new_revision_preserves_old_and_does_not_rewrite_current(catalog):
    old = ProblemVersion.objects.get(problem_id="sum-positive")
    changed = copy.deepcopy(catalog)
    changed["problems"][0]["revision"] = old.revision + 1
    changed["problems"][0]["statement"] += " 新说明。"
    import_package(changed)
    current = Problem.objects.get(id="sum-positive").current
    assert current.revision == old.revision + 1 and current.digest != old.digest
    assert ProblemVersion.objects.get(pk=old.pk).data == old.data
    with pytest.raises(ContentError, match="roll back"):
        import_package(catalog)


def test_public_projection_excludes_protected_material(catalog):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    data = public_problem(version)
    assert "reference" not in data and "tests" not in data
    assert all(case["public"] for case in data["examples"])
    assert "全非正数" not in json.dumps(data, ensure_ascii=False)


def test_withdrawal_preserves_history(catalog):
    data = copy.deepcopy(catalog)
    data["problems"] = []
    data["nodes"] = [n for n in data["nodes"] if n["kind"] != "path"]
    import_package(data)
    assert not Problem.objects.filter(published=True).exists()
    assert ProblemVersion.objects.count() == len(catalog["problems"])
