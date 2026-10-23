import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.content import ContentError, public_execution, public_problem, validate_package
from core.resources import BUDGET_POLICY, reservation_units
from core.result_evidence import InvalidResult, verified_evidence
from judge import object_api
from judge.evaluator import evaluate
from judge.sandbox import DockerSandbox, Execution, SandboxUnavailable
from judge.wasi import WasiSandbox
from scripts import practice


@pytest.fixture
def api_problem():
    data = json.loads(Path("content/starter.json").read_text())
    return next(p for p in data["problems"] if p["id"] == "bounded-queue-api")


@pytest.fixture
def api_package(api_problem):
    version = SimpleNamespace(data=api_problem)
    return {
        "format": 1,
        "version_id": 1,
        "source_digest": "a" * 64,
        "problem": public_problem(version),
        "formal_result": False,
        "public_tests": [
            {key: value for key, value in case.items() if key != "public"}
            for case in public_execution(version)["tests"]
        ],
    }


def test_published_api_and_public_projection_have_separate_contracts(api_problem):
    object_api.validate_problem(api_problem)
    public = object_api.project_public_problem(api_problem)
    object_api.validate_problem(public, public_only=True)
    assert len(public["tests"]) == 2 and len(api_problem["tests"]) == 8
    assert "reference" not in public
    assert all(case["public"] for case in public["tests"])
    for hidden in api_problem["tests"][2:]:
        assert hidden["name"] not in json.dumps(public, ensure_ascii=False)
    with pytest.raises(object_api.APIContractError):
        object_api.validate_problem(public)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(function="solve"),
        lambda p: p.update(output_format="json"),
        lambda p: p.update(language="javascript"),
        lambda p: p["interface"].update(class_name="__import__"),
        lambda p: p["interface"].update(constructor_arity=True),
        lambda p: p["interface"].update(import_path="anything"),
        lambda p: p["interface"]["methods"].update(__getattribute__={"arity": 1, "raises": []}),
        lambda p: p["interface"]["methods"]["get"].update(raises=["MemoryError"]),
        lambda p: p["interface"]["methods"]["get"].update(raises=["IndexError", "IndexError"]),
        lambda p: p["comparison"].update(absolute=False),
        lambda p: p["tests"][0].update(public=1),
        lambda p: p["tests"][0].update(operations=None),
        lambda p: p["tests"][0]["operations"][1].update(call="unknown"),
        lambda p: p["tests"][0]["operations"][1].update(method="__dict__"),
        lambda p: p["tests"][0]["operations"][1].update(args=[]),
        lambda p: p["tests"][0]["operations"][0].update(extra=True),
        lambda p: p["tests"][0]["operations"].insert(1, {"new": "q", "args": [2]}),
        lambda p: p["tests"][0]["expected"].__setitem__(0, {"value": 1}),
        lambda p: p["tests"][0]["expected"].__setitem__(1, {"raises": "KeyError"}),
        lambda p: p["tests"][0]["expected"].__setitem__(1, {"value": float("nan")}),
    ],
)
def test_malformed_interface_operations_and_expectations_are_rejected(api_problem, change):
    change(api_problem)
    with pytest.raises(object_api.APIContractError):
        object_api.validate_problem(api_problem)


def test_input_instance_operation_and_shape_budgets(api_problem):
    interface = api_problem["interface"]
    operations = api_problem["tests"][-1]["operations"]
    assert len(operations) == 64
    object_api.validate_operations(interface, operations)
    with pytest.raises(object_api.APIContractError):
        object_api.validate_operations(interface, operations + [operations[-1]])
    objects = [{"new": f"a{i}", "args": [1]} for i in range(9)]
    with pytest.raises(object_api.APIContractError):
        object_api.validate_operations(
            interface, objects + [{"call": "a0", "method": "size", "args": []}]
        )
    for value in ([0] * 5000, "a" * 24001, 10**400, {1: "not a JSON key"}, (1,)):
        with pytest.raises(object_api.APIContractError):
            object_api.bounded_value(value)


def test_schema_rejects_mixed_api_fields_on_other_evaluators(package, api_problem):
    package["problems"][0]["interface"] = api_problem["interface"]
    with pytest.raises(ContentError):
        validate_package(package)


def test_host_grades_complete_transcript_without_sending_expectations(api_problem):
    sandbox = Mock()
    sandbox.execute_api.side_effect = [
        Execution("ok", {"status": "ok", "results": c["expected"]}, 7) for c in api_problem["tests"]
    ]
    checkpoint = Mock()
    result = evaluate(api_problem, "private learner code", "submit", sandbox, checkpoint)
    assert (result["verdict"], result["passed"], result["total"]) == ("accepted", 8, 8)
    assert result["elapsed_ms"] == 56 and checkpoint.call_count == 8
    assert len(result["public_cases"]) == 2
    for call, case in zip(sandbox.execute_api.call_args_list, api_problem["tests"], strict=True):
        assert call.args == ("private learner code", api_problem["interface"], case["operations"])
    assert "expected" not in str(sandbox.execute_api.call_args_list)
    assert api_problem["tests"][-1]["name"] not in str(result)


@pytest.mark.parametrize(
    "wire",
    [
        {"verdict": "accepted", "score": 100},
        {"status": []},
        {"status": "ok", "results": []},
        {"status": "accepted"},
        {"status": "ok", "results": [], "expected": "forged"},
    ],
)
def test_guest_cannot_supply_a_verdict_or_incomplete_transcript(api_problem, wire):
    sandbox = Mock()
    sandbox.execute_api.return_value = Execution("ok", wire)
    result = evaluate(api_problem, "source", "submit", sandbox)
    assert result["verdict"] == "wrong_answer" and result["passed"] == 0


@pytest.mark.parametrize("verdict", sorted(object_api.FAILURES))
def test_runtime_failures_remain_distinct(api_problem, verdict):
    sandbox = Mock()
    sandbox.execute_api.return_value = Execution(verdict)
    assert evaluate(api_problem, "source", "run", sandbox)["verdict"] == verdict


@pytest.mark.parametrize("status", ["syntax_error", "runtime_error", "memory_limit"])
def test_guest_compilation_and_api_failures_map_to_safe_categories(api_problem, status):
    sandbox = Mock()
    sandbox.execute_api.return_value = Execution("ok", {"status": status})
    assert evaluate(api_problem, "source", "run", sandbox)["verdict"] == status


def test_cancel_and_platform_errors_do_not_become_wrong_answers(api_problem):
    sandbox = Mock()
    checkpoint = Mock(side_effect=RuntimeError("lease lost"))
    with pytest.raises(RuntimeError, match="lease lost"):
        evaluate(api_problem, "source", "submit", sandbox, checkpoint)
    sandbox.execute_api.assert_not_called()
    sandbox.execute_api.side_effect = SandboxUnavailable("control plane")
    with pytest.raises(SandboxUnavailable):
        evaluate(api_problem, "source", "run", sandbox)


def test_only_public_scenarios_run_in_local_mode(api_problem):
    public = object_api.project_public_problem(api_problem)
    sandbox = Mock()
    sandbox.execute_api.side_effect = [
        Execution("ok", {"status": "ok", "results": c["expected"]}) for c in public["tests"]
    ]
    result = object_api.evaluate_public(public, "source", sandbox)
    assert result["passed"] == result["total"] == 2


def test_backend_selection_never_executes_api_on_host_or_gvisor(api_problem, monkeypatch):
    gvisor = DockerSandbox("python@sha256:" + "a" * 64)
    command = Mock(side_effect=AssertionError("must not start a container"))
    monkeypatch.setattr(gvisor, "command", command)
    with pytest.raises(SandboxUnavailable):
        evaluate(api_problem, "source", "run", gvisor)
    command.assert_not_called()
    wasi = WasiSandbox(Path("unused"), "a" * 64)
    execute = Mock(return_value=Execution("ok", {}))
    monkeypatch.setattr(wasi, "_execute", execute)
    operations = api_problem["tests"][0]["operations"]
    wasi.execute_api("source", api_problem["interface"], operations)
    assert execute.call_args.args == (
        object_api.DRIVER,
        object_api.ENTRYPOINT,
        ["source", api_problem["interface"], operations],
        "python",
    )


def test_cli_accepts_only_the_public_object_api_contract(api_package):
    assert practice.validate_package(api_package) == api_package
    payload = practice.api_payload(api_package)
    assert all(c["public"] for c in payload["tests"])
    for change in [
        lambda p: p["problem"].update(function="solve"),
        lambda p: p["problem"].update(assessment={}),
        lambda p: p["public_tests"][0].update(public=False),
        lambda p: p["public_tests"][0]["operations"][1].update(method="__class__"),
    ]:
        bad = copy.deepcopy(api_package)
        change(bad)
        with pytest.raises(practice.PracticeError):
            practice.validate_package(bad)


@pytest.mark.parametrize(
    "damage", [None, "legacy", "evaluator", "language", "units", "run", "fingerprint"]
)
def test_api_evidence_requires_explicit_runtime_and_consumed_reservation(api_problem, damage):
    from django.utils import timezone

    now = timezone.now()
    version = SimpleNamespace(data=api_problem)
    assert reservation_units(version, "run") == 2
    assert reservation_units(version, "submit") == 8
    # Persisted worker shape only; real execution is covered separately.
    job = SimpleNamespace(
        mode="submit",
        status="finished",
        verdict="accepted",
        version=version,
        started_at=now - timedelta(seconds=2),
        execution_started_at=now - timedelta(seconds=1),
        finished_at=now,
        budget_state="consumed",
        budget_units=8,
        budget_policy=BUDGET_POLICY,
        result={"verdict": "accepted", "passed": 8, "total": 8},
        environment={
            "policy": "wasi-functions-v2",
            "policy_digest": "a" * 64,
            "bundle": "b" * 64,
            "python": "3.14.9",
            "quickjs": "2026-06-04",
            "wasmtime": "50.0.1",
            "languages": ["python", "javascript"],
            "language": "python",
            "evaluator": object_api.CONTRACT,
        },
        evaluation_fingerprint=hashlib.sha256(f"wasi:{'b' * 64}:{'a' * 64}".encode()).hexdigest(),
    )
    if damage is None:
        assert verified_evidence(job) == job.result
        return
    if damage == "legacy":
        job.budget_state = "legacy"
    elif damage in {"evaluator", "language"}:
        del job.environment[damage]
    elif damage == "units":
        job.budget_units = 2
    elif damage == "run":
        job.mode = "run"
    else:
        job.evaluation_fingerprint = "f" * 64
    with pytest.raises(InvalidResult):
        verified_evidence(job)


@pytest.mark.django_db
def test_hint_preview_contains_public_interface_without_source_or_private_cases(
    catalog, user, monkeypatch
):
    from coaching import configuration, hints
    from core.models import Problem

    monkeypatch.setattr(
        configuration,
        "current",
        lambda: SimpleNamespace(
            identity="a" * 64,
            description="Preview fixture; no provider calls",
            provider=SimpleNamespace(model="fixture"),
        ),
    )
    version = Problem.objects.get(pk="bounded-queue-api").current
    request = hints.preview(user, version.pk, question="How do I test independent queues?")
    assert request.payload["problem"]["interface"] == version.data["interface"]
    assert not request.payload["source_opt_in"] and "learner_source" not in request.payload
    serialized = json.dumps(request.payload, ensure_ascii=False)
    assert version.data["reference"] not in serialized
    assert all(
        case["name"] not in serialized for case in version.data["tests"] if not case["public"]
    )


@pytest.mark.django_db
def test_web_api_editor_filter_and_public_package(catalog, client, settings):
    response = client.get("/practice/", {"evaluator": object_api.CONTRACT})
    assert [p["id"] for p in response.context["page"]] == ["bounded-queue-api"]
    response = client.get("/problems/bounded-queue-api/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "定义 BoundedQueue 类及声明的方法" in body
    assert "q.put(7)" in body and "库/API 类" in body
    assert "定义  函数" not in body
    package = client.get("/api/v1/problems/bounded-queue-api/").json()
    assert practice.validate_package(package) == package
    assert len(package["public_tests"]) == 2
    assert "完整六十四步状态序列" not in json.dumps(package, ensure_ascii=False)
    settings.JUDGE_BACKEND = "gvisor"
    unsupported = client.get("/practice/", {"evaluator": object_api.CONTRACT, "supported": "0"})
    assert unsupported.context["page"].paginator.count == 1
