"""Content/publication tests only. No CUDA is compiled or executed here."""

import copy
import io
import json
import stat
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from core.content import ContentError, compose_package, digest, import_package, validate_package
from core.models import GPUDevice, Problem, ProblemVersion, Submission
from judge.evaluator import evaluate
from judge.gpu_contract import compile_request
from judge.sandbox import SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def complete_cuda_package():
    return compose_package(ROOT / "content/starter.json", [ROOT / "content/candidates/cuda.json"])


@pytest.fixture
def small_cuda_package(complete_cuda_package):
    data = copy.deepcopy(complete_cuda_package)
    data["problems"] = [p for p in data["problems"] if p["language"] == "cuda"]
    data["nodes"] = [n for n in data["nodes"] if n["kind"] != "path"]
    for problem in data["problems"]:
        problem["tests"] = [problem["tests"][0], problem["tests"][-1]]
    return data


def test_candidates_preserve_base_and_cover_declared_boundaries(package, complete_cuda_package):
    combined = complete_cuda_package
    assert combined["nodes"][: len(package["nodes"])] == package["nodes"]
    assert combined["problems"][: len(package["problems"])] == package["problems"]
    vector, matrix = combined["problems"][-2:]
    assert vector["tests"][0]["expected"] == [4, 2, 0, 0]
    assert matrix["tests"][0]["expected"] == [58, 64, 139, 154]
    assert {len(case["inputs"]["a"]) for case in vector["tests"]} >= {0, 1, 257, 4096}
    assert any(
        [case["inputs"][key] for key in ("m", "n", "k")] == [64] * 3 for case in matrix["tests"]
    )
    assert any(
        len({case["inputs"][key] for key in ("m", "n", "k")}) == 3 for case in matrix["tests"]
    )
    mistakes = json.loads((ROOT / "tests/cuda_incorrect_solutions.json").read_text())
    assert set(mistakes) == {vector["id"], matrix["id"]}
    for problem in (vector, matrix):
        variants = mistakes[problem["id"]]
        assert len(variants) >= 2
        assert (
            len({row["source"] for row in variants} | {problem["reference"]}) == len(variants) + 1
        )
        assert all(row["mistake"].strip() for row in variants)
        for row in variants:
            compile_request(row["source"], problem["operation"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "arbitrary-code"),
        ("entrypoint", "main"),
        ("language", "python"),
        ("function", "solve"),
        ("output_format", "json"),
        ("model_kind", "linear-regression-v1"),
        ("uncertainty_model", "known-independent-sigma-v1"),
        ("assessment", {"kind": "approved-ledger-v1", "resource_profile": "wasi-agent-v1"}),
        ("comparison", {"kind": "float", "absolute": 0.010001, "relative": 0}),
        ("comparison", {"kind": "numeric", "absolute": 0.001, "relative": 0}),
        ("starter", "中" * 6667),
        ("reference", "x\0y"),
        ("reference", "中" * 6667),
    ],
)
def test_cuda_rejects_mixed_contracts_and_byte_budget(small_cuda_package, field, value):
    small_cuda_package["problems"][0][field] = value
    with pytest.raises(ContentError):
        validate_package(small_cuda_package)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["tests"][0].update(expected=[0, 0, 0, 0]),
        lambda p: p["tests"][0].update(expected=[float("inf")] * 4),
        lambda p: p["tests"][0].update(expected=[True] * 4),
        lambda p: p["tests"][0]["inputs"].update(b=[1]),
        lambda p: p["tests"][0]["inputs"].update(checker="user-defined"),
        lambda p: p["tests"][0]["inputs"].update(a=[10001] * 4),
        lambda p: p["tests"][0].update(name="x" * 81),
        lambda p: p["tests"][-1].update(public=True),
        lambda p: p["tests"][0].update(args=[]),
        lambda p: p["tests"].extend(copy.deepcopy(p["tests"]) * 4),
    ],
)
def test_cuda_rejects_invalid_cases_before_import(small_cuda_package, mutation):
    mutation(small_cuda_package["problems"][0])
    with pytest.raises(ContentError):
        validate_package(small_cuda_package)


@pytest.mark.parametrize("value", [True, 0, 65, 2.5])
def test_cuda_dimensions_must_fit_runtime(small_cuda_package, value):
    small_cuda_package["problems"][1]["tests"][0]["inputs"]["m"] = value
    with pytest.raises(ContentError):
        validate_package(small_cuda_package)


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "vector-add-f32-v1"),
        ("entrypoint", "solve"),
        ("comparison", {"kind": "float", "absolute": 0, "relative": 0}),
        ("comparison", {"kind": "numeric", "absolute": 0.01, "relative": 0}),
    ],
)
def test_existing_cpu_contract_does_not_widen(package, field, value):
    # Exercise every existing CPU type, not only the generic function branch.
    for evaluator in {problem["evaluator"] for problem in package["problems"]}:
        data = copy.deepcopy(package)
        next(p for p in data["problems"] if p["evaluator"] == evaluator)[field] = value
        with pytest.raises(ContentError):
            validate_package(data)


def test_gpu_never_enters_cpu_dispatch(small_cuda_package):
    problem = small_cuda_package["problems"][0]
    with pytest.raises((ValueError, SandboxUnavailable)):
        evaluate(problem, problem["reference"], "submit", object())


def test_compose_rejects_collision_and_unresolved_reference(tmp_path, package):
    addition = tmp_path / "extra.json"
    for data in (
        {"schema_version": 1, "nodes": [package["nodes"][0]], "problems": []},
        {
            "schema_version": 1,
            "nodes": [],
            "problems": [dict(package["problems"][0], id="new", concepts=["absent"])],
        },
        {"schema_version": 1, "nodes": [], "problems": [], "untrusted": True},
    ):
        addition.write_text(json.dumps(data))
        with pytest.raises(ContentError):
            compose_package(settings.CONTENT_ROOT / "starter.json", [addition])
    addition.write_text('{"schema_version":1,"nodes":[],"nodes":[],"problems":[]}')
    with pytest.raises(ContentError, match="Duplicate JSON"):
        compose_package(settings.CONTENT_ROOT / "starter.json", [addition])


def test_compose_command_has_private_exclusive_output_and_needs_no_database(tmp_path):
    output = tmp_path / "candidate.json"
    stdout = io.StringIO()
    arguments = dict(
        base=str(ROOT / "content/starter.json"),
        additions=[str(ROOT / "content/candidates/cuda.json")],
        output=str(output),
    )
    call_command("content_compose", **arguments, stdout=stdout)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    assert b"cuda-kernel-v1" in original
    assert "Database untouched; code not executed" in stdout.getvalue()
    with pytest.raises(CommandError):
        call_command("content_compose", **arguments)
    assert output.read_bytes() == original


def test_gpu_import_uses_immutable_versions_and_withdrawal(catalog, complete_cuda_package):
    old = dict(ProblemVersion.objects.values_list("pk", "digest"))
    candidate = copy.deepcopy(complete_cuda_package)
    import_package(candidate)
    assert dict(ProblemVersion.objects.filter(pk__in=old).values_list("pk", "digest")) == old
    original = Problem.objects.get(pk="cuda-vector-add").current
    changed = copy.deepcopy(candidate)
    changed["problems"][-2]["summary"] += " 已修订。"
    with pytest.raises(ContentError, match="Immutable revision"):
        import_package(changed)
    original.refresh_from_db()
    assert original.digest == digest(candidate["problems"][-2])
    changed["problems"][-2]["revision"] += 1
    import_package(changed)
    assert Problem.objects.get(pk="cuda-vector-add").current.revision == 2
    import_package(catalog)
    assert not Problem.objects.get(pk="cuda-vector-add").published
    assert ProblemVersion.objects.filter(problem_id="cuda-vector-add").count() == 2


def test_import_does_not_enable_gpu_or_expose_private_content(
    catalog, complete_cuda_package, logged_client, user, settings
):
    from practice_api.services import issue_token

    settings.JUDGE_ENABLED = True
    settings.JUDGE_GPU_DEVICES = ()
    import_package(complete_cuda_package)
    problem = Problem.objects.get(pk="cuda-vector-add")
    package = logged_client.get(reverse("practice_api:problem", args=[problem.pk]))
    assert package.status_code == 200
    public = package.json()
    assert public["source_digest"] == problem.current.digest
    assert "reference" not in public["problem"]
    assert len(public["public_tests"]) == 2
    assert all("public" not in case for case in public["public_tests"])
    for case in problem.current.data["tests"]:
        if not case["public"]:
            assert case["name"] not in package.content.decode()
    page = logged_client.get(reverse("problem", args=[problem.pk]))
    assert page.status_code == 200
    _, raw = issue_token(user, "CUDA review fixture", 30, can_submit=True, can_read_results=True)
    response = logged_client.post(
        reverse("practice_api:submit", args=[problem.pk]),
        {
            "version_id": problem.current_id,
            "source": problem.current.data["reference"],
            "mode": "submit",
        },
        content_type="application/json",
        headers={"Authorization": "Bearer " + raw, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 503
    assert not GPUDevice.objects.exists()
    assert not Submission.objects.exists()
