"""Agent admission and persisted evidence boundaries; fixtures are not execution claims."""

import copy
import hashlib
import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.content import digest, validate_package
from core.management.commands.sandbox_check import probes_for, suite_digest
from core.models import Problem, ProblemVersion, Submission
from core.resources import reservation_units
from core.result_evidence import InvalidResult, safe_result, verified_evidence, verified_metrics
from judge.agent import Ledger
from judge.agent_policy import PROFILE, profile_digest
from judge.agent_probes import PROBLEM, REFERENCE
from judge.results import LABELS
from scripts.practice import validate_package as validate_cli_package


@pytest.fixture
def version(catalog):
    data = {
        **copy.deepcopy(catalog["problems"][0]),
        **copy.deepcopy(PROBLEM),
        "id": "agent-fixture",
        "revision": 1,
        "title": "受控账目工具任务",
        "starter": "def act(observation,state):\n    pass\n",
        "reference": REFERENCE,
    }
    data["comparison"] = {"kind": "exact", "absolute": 0, "relative": 0}
    validate_package({**catalog, "problems": catalog["problems"] + [data]})
    problem = Problem.objects.create(id=data["id"], published=True)
    result = ProblemVersion.objects.create(
        problem=problem, revision=1, digest=digest(data), data=data
    )
    problem.current = result
    problem.save()
    return result


def result_fixture(version):
    feedback = []
    for case in version.data["tests"]:
        if not case["public"]:
            continue
        records = case["environment"]["records"]
        ledger = Ledger(records)
        actions = [{"tool": "list_records", "args": {}, "state": None}]
        actions += [
            {"tool": "read_record", "args": {"id": row["id"]}, "state": None} for row in records
        ]
        actions += [
            {
                "tool": "submit_report",
                "args": {
                    "total": sum(row["amount"] for row in records if row["approved"]),
                    "count": sum(row["approved"] for row in records),
                },
                "state": None,
            }
        ]
        for turn, action in enumerate(actions, 1):
            ledger.apply(action, turn)
        turns = len(actions)
        feedback.append(
            {
                "name": case["name"],
                "verdict": "accepted",
                "label": LABELS["accepted"],
                "reason": "completed",
                "metrics": {
                    "resource_profile": PROFILE["id"],
                    "profile_digest": profile_digest(),
                    "turns": turns,
                    "tool_cost": ledger.cost,
                    "cpu_ms": 1000,
                    "fuel_reserved": turns * PROFILE["turn_fuel"],
                    "output_reserved": turns * PROFILE["turn_output_bytes"],
                    "trajectory": ledger.trajectory,
                },
            }
        )
    return {
        "verdict": "accepted",
        "label": LABELS["accepted"],
        "passed": len(version.data["tests"]),
        "total": len(version.data["tests"]),
        "elapsed_ms": 100,
        "public_cases": feedback,
    }


def job_fixture(user, version):
    now = timezone.now()
    return Submission.objects.create(
        user=user,
        version=version,
        source=REFERENCE,
        source_digest="c" * 64,
        mode="submit",
        status="finished",
        verdict="accepted",
        started_at=now - timedelta(seconds=1),
        finished_at=now,
        result=result_fixture(version),
        environment={
            "policy": "wasi-functions-v2",
            "policy_digest": "a" * 64,
            "bundle": "b" * 64,
            "python": "3.14.9",
            "quickjs": "2026-06-04",
            "wasmtime": "50.0.1",
            "languages": ["python", "javascript"],
            "language": "python",
            "evaluator": "python-agent-tools-v1",
        },
        evaluation_fingerprint=hashlib.sha256(f"wasi:{'b' * 64}:{'a' * 64}".encode()).hexdigest(),
    )


def test_agent_public_package_and_fixed_cost(version, client):
    response = client.get(reverse("practice_api:problem", args=[version.problem_id]))
    package = response.json()
    validate_cli_package(package)
    text = json.dumps(package)
    assert "hidden-episode-canary" not in text and "hidden-record-canary" not in text
    assert reservation_units(version, "submit") == 12 and reservation_units(version, "run") == 6
    version.data["assessment"]["cost"] = 0
    assert reservation_units(version, "submit") == 12
    with pytest.raises(ValueError):
        validate_cli_package(
            {**package, "public_tests": [{**package["public_tests"][0], "public": False}]}
        )


def test_agent_progress_private_trace_and_no_board(version, user, other_user, logged_client):
    job = job_fixture(user, version)
    assert verified_evidence(job)["assessment"]["kind"] == "approved-ledger-v1"
    with pytest.raises(InvalidResult):
        verified_metrics(job)
    assert logged_client.get(reverse("learning:index")).context["summary"]["completed"] == 1
    page = logged_client.get(reverse("submission", args=[job.pk]))
    assert page.status_code == 200 and "实际工具结果" in page.content.decode()
    assert "hidden-record-canary" not in page.content.decode()
    assert "选择公开这条评测结果" not in page.content.decode()
    logged_client.force_login(other_user)
    assert logged_client.get(reverse("submission", args=[job.pk])).status_code == 404


@pytest.mark.parametrize(
    "damage",
    ["state", "hidden", "forged_result", "bool_cost", "profile", "missing_read", "turns", "reason"],
)
def test_inconsistent_or_private_agent_trace_is_not_progress(version, user, damage):
    job = job_fixture(user, version)
    row = job.result["public_cases"][0]
    metrics = row["metrics"]
    if damage == "state":
        metrics["trajectory"][0]["state"] = "PRIVATE_STATE"
    elif damage == "hidden":
        row["name"] = "hidden-episode-canary"
    elif damage == "forged_result":
        metrics["trajectory"][1]["result"]["amount"] += 1
    elif damage == "bool_cost":
        metrics["trajectory"][0]["cost"] = True
    elif damage == "profile":
        metrics["profile_digest"] = "f" * 64
    elif damage == "missing_read":
        metrics["trajectory"].pop(1)
    elif damage == "turns":
        metrics["turns"] += 1
    elif damage == "reason":
        row["reason"] = "incorrect_report"
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert safe_result(job) == {}


def test_all_professional_probes_are_part_of_receipt_identity():
    identity = {"policy": "wasi-functions-v2"}
    probes = probes_for(identity)
    assert len(probes) == 129
    assert len([p for p in probes if p[0].startswith("api/")]) == 6
    assert len([p for p in probes if p[0].startswith("agent/")]) == 16
    assert {p[0] for p in probes if p[0].startswith(("ml/", "science/"))} == {
        "ml/reference",
        "ml/forged-score",
        "science/reference",
        "science/invented-covariance",
    }
    assert len(suite_digest(identity)) == 64
    assert len(probes_for({"policy": "gvisor-python-function-v1"})) == 15


def test_agent_case_count_is_bounded_by_trusted_registry(version):
    version.data["tests"] *= 5
    with pytest.raises(ValueError):
        reservation_units(version, "submit")


def test_lease_renewal_budget_exceeds_each_agent_turn(settings):
    assert settings.JUDGE_LEASE_SECONDS > PROFILE["episode_wall_seconds"] + 3
    assert PROFILE["max_episodes"] * PROFILE["episode_wall_seconds"] == 160


def test_receipt_without_any_specialist_group_cannot_start_worker(settings, tmp_path):
    import time

    from core.management.commands.sandbox_check import check_receipt
    from judge.sandbox import SandboxUnavailable

    identity = {"policy": "wasi-functions-v2", "bundle": "a" * 64}
    path = tmp_path / "receipt.json"
    settings.JUDGE_RECEIPT = str(path)
    names = [probe[0] for probe in probes_for(identity)]
    for group in ("agent/", "ml/", "science/", "api/"):
        path.write_text(
            json.dumps(
                {
                    "created": time.time(),
                    "identity": identity,
                    "suite": suite_digest(identity),
                    "passed": [name for name in names if not name.startswith(group)],
                }
            )
        )
        with pytest.raises(SandboxUnavailable):
            check_receipt(identity)


@pytest.mark.sandbox
def test_actual_agent_result_and_public_api_cli_chain(version, user, client, tmp_path, settings):
    import os

    from django.core.management import call_command

    from core.models import WorkerHealth
    from core.queue import configured_fingerprint, enqueue
    from scripts import practice

    if os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi":
        pytest.skip("Requires explicit real WASI; skipped is not execution evidence")
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="actual-agent-admission",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    job = enqueue(user, version, REFERENCE, "submit")
    call_command("judge_worker", once=True, worker_id="actual-agent-worker")
    job.refresh_from_db()
    assert job.result["verdict"] == "accepted" and job.budget_state == "consumed"
    assert job.budget_units == 12 and job.execution_started_at is not None
    assert verified_evidence(job)["assessment"]["resource_profile"] == "wasi-agent-v1"
    package = client.get(reverse("practice_api:problem", args=[version.problem_id])).json()
    downloader = practice.Client("https://example.test")
    downloader.request = lambda path: copy.deepcopy(package)
    directory = tmp_path / "exercise"
    practice.download(downloader, version.problem_id, directory)
    (directory / "solution.py").write_text(REFERENCE)
    output = practice.local_test(
        directory, os.environ["JUDGE_WASI_BUNDLE"], os.environ["JUDGE_WASI_SHA256"]
    )
    assert output["formal_result"] is False and output["result"]["verdict"] == "accepted"
    assert output["result"]["total"] == 1


def test_eight_episode_reservation_is_48_and_third_submit_is_rejected(catalog, user, settings):
    from core.models import WorkerHealth
    from core.queue import AdmissionError, configured_fingerprint, enqueue
    from core.resources import usage

    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_MAX_PENDING = 100
    settings.JUDGE_DAILY_CPU_UNITS = 120
    WorkerHealth.objects.create(
        id="agent-quota",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )
    version = Problem.objects.get(pk="approved-ledger-report").current
    first = enqueue(user, version, REFERENCE, "submit")
    second = enqueue(user, version, REFERENCE, "submit")
    assert first.budget_units == second.budget_units == 48
    with pytest.raises(AdmissionError) as failure:
        enqueue(user, version, REFERENCE, "submit")
    assert failure.value.status == 429 and usage(user)["remaining"] == 24
