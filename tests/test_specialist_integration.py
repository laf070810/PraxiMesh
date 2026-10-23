"""Persisted worker-contract boundaries; these fixtures do not execute learner code."""

import copy
import hashlib
import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Problem, Submission
from core.result_evidence import InvalidResult, safe_result, verified_evidence, verified_metrics
from judge.results import LABELS
from judge.scientific import assess_fit, fit_oracle
from practice_api.services import issue_token
from scripts.practice import validate_package as validate_cli_package

SLUGS = ["train-linear-model", "weighted-line-fit"]


def result_fixture(version):
    ml = version.data["evaluator"] == "python-ml-linear-v1"
    feedback = []
    for case in version.data["tests"]:
        if not case["public"]:
            continue
        row = {"name": case["name"], "verdict": "accepted", "label": LABELS["accepted"]}
        if ml:
            row.update(metrics={"mse": 0.0, "mae": 0.0}, thresholds=case["thresholds"])
        else:
            oracle = fit_oracle(case["observations"])
            model = {key: oracle[key] for key in ["parameters", "covariance"]}
            assessment = assess_fit(model, case["observations"])
            assert assessment["accepted"]
            row.update({key: assessment[key] for key in ["checks", "metrics", "model_diagnostic"]})
        feedback.append(row)
    assessment = {
        "kind": "linear-regression-v1" if ml else "known-independent-sigma-v1",
        "metric_version": "mse-mae-v1" if ml else "weighted-line-diagnostics-v1",
        "visibility": "public-cases-only",
    }
    if not ml:
        assessment["diagnostic_is_correctness"] = False
    return {
        "verdict": "accepted",
        "label": LABELS["accepted"],
        "passed": len(version.data["tests"]),
        "total": len(version.data["tests"]),
        "elapsed_ms": 10,
        "public_cases": feedback,
        "assessment": assessment,
    }


def job_fixture(user, version, **overrides):
    environment = {
        "policy": "wasi-functions-v2",
        "policy_digest": "a" * 64,
        "bundle": "b" * 64,
        "python": "3.14.9",
        "quickjs": "2026-06-04",
        "wasmtime": "50.0.1",
        "languages": ["python", "javascript"],
        "language": "python",
        "evaluator": version.data["evaluator"],
    }
    now = timezone.now()
    return Submission.objects.create(
        **{
            "user": user,
            "version": version,
            "source": "PRIVATE SOURCE",
            "source_digest": "c" * 64,
            "mode": "submit",
            "status": "finished",
            "started_at": now - timedelta(seconds=1),
            "finished_at": now,
            "verdict": "accepted",
            "result": result_fixture(version),
            "environment": environment,
            "evaluation_fingerprint": hashlib.sha256(
                f"wasi:{'b' * 64}:{'a' * 64}".encode()
            ).hexdigest(),
            **overrides,
        }
    )


@pytest.mark.parametrize("slug", SLUGS)
def test_explicit_public_contract_is_cli_compatible_and_hidden_free(catalog, client, slug):
    version = Problem.objects.get(pk=slug).current
    response = client.get(reverse("practice_api:problem", args=[slug]))
    data = response.json()
    validate_cli_package(data)
    text = json.dumps(data)
    for case in version.data["tests"]:
        if not case["public"]:
            assert case["name"] not in text and case["dataset"]["digest"] not in text
    assert "reference" not in data["problem"] and not data["formal_result"]
    poisoned = copy.deepcopy(data)
    poisoned["public_tests"][0]["public"] = False
    with pytest.raises(ValueError):
        validate_cli_package(poisoned)
    page = client.get(f"/problems/{slug}/")
    assert page.status_code == 200 and "数据集身份" in page.content.decode()
    assert "返回结果：None" not in page.content.decode()


@pytest.mark.parametrize("slug", SLUGS)
def test_specialist_completion_is_private_verified_and_not_a_pass_count_board(
    catalog, logged_client, user, other_user, slug
):
    version = Problem.objects.get(pk=slug).current
    job = job_fixture(user, version)
    assert verified_evidence(job)["verdict"] == "accepted"
    with pytest.raises(InvalidResult):
        verified_metrics(job)
    summary = logged_client.get(reverse("learning:index")).context["summary"]
    assert summary["completed"] == 1 and summary["attempts"] == 1
    page = logged_client.get(reverse("submission", args=[job.pk]))
    assert page.status_code == 200
    assert "仅展示公开场景" in page.content.decode()
    assert "选择公开这条评测结果" not in page.content.decode()
    assert logged_client.get(reverse("showcase:share", args=[job.pk])).status_code == 400
    logged_client.force_login(other_user)
    assert logged_client.get(reverse("submission", args=[job.pk])).status_code == 404


@pytest.mark.parametrize(
    "damage",
    ["empty", "hidden", "extra", "count", "bool", "fingerprint", "missing_environment", "run"],
)
def test_invalid_specialist_payload_cannot_create_learning_evidence(
    catalog, logged_client, user, damage
):
    version = Problem.objects.get(pk=SLUGS[0]).current
    job = job_fixture(user, version)
    if damage == "empty":
        job.result = {}
    elif damage == "hidden":
        job.result["public_cases"][0]["name"] = next(
            case["name"] for case in version.data["tests"] if not case["public"]
        )
    elif damage == "extra":
        job.result["public_cases"][0]["model"] = "HIDDEN_MODEL_SENTINEL"
    elif damage == "count":
        job.result["passed"] -= 1
    elif damage == "bool":
        job.result["public_cases"][0]["metrics"]["mse"] = True
    elif damage == "fingerprint":
        job.evaluation_fingerprint = "f" * 64
    elif damage == "missing_environment":
        job.environment = {}
    elif damage == "run":
        job.mode = "run"
    job.save()
    assert logged_client.get(reverse("learning:index")).context["summary"]["completed"] == 0
    if damage in {"empty", "hidden", "extra", "count", "bool", "run"}:
        assert safe_result(job) == {}
    _, raw = issue_token(user, "test", 1, can_submit=False, can_read_results=True)
    data = logged_client.get(
        reverse("practice_api:result", args=[job.pk]), headers={"Authorization": "Bearer " + raw}
    ).json()
    assert not data["formal_result"] and "HIDDEN_MODEL_SENTINEL" not in json.dumps(data)


def test_empty_old_result_no_longer_counts_as_formal(catalog, logged_client, user):
    version = Problem.objects.get(pk="sum-positive").current
    Submission.objects.create(
        user=user,
        version=version,
        source="secret",
        source_digest="a" * 64,
        mode="submit",
        status="finished",
        verdict="accepted",
    )
    assert logged_client.get(reverse("learning:index")).context["summary"]["attempts"] == 0
    assert logged_client.get("/practice/").status_code == 200


@pytest.mark.parametrize("slug", SLUGS)
def test_specialist_pinned_project_progress_survives_current_problem_revision(
    catalog, user, other_user, slug
):
    from core.content import digest
    from core.models import ProblemVersion
    from learning.evidence import build_evidence
    from projects.models import Project, ProjectRevision, ProjectStage
    from projects.progress import stage_cards

    version = Problem.objects.get(pk=slug).current
    job = job_fixture(user, version)
    project = Project.objects.create(id="specialist-project", published=True)
    revision = ProjectRevision.objects.create(project=project, revision=1, digest="a" * 64, data={})
    ProjectStage.objects.create(
        revision=revision,
        key="fit",
        position=0,
        problem_version=version,
        data={
            "title": "Professional task",
            "description": "A pinned fitting stage.",
            "final": True,
            "recommended": [],
        },
    )
    assert stage_cards(revision, user)[0]["evidence"]["id"] == job.pk
    assert stage_cards(revision, other_user)[0]["evidence"] is None
    changed = copy.deepcopy(version.data)
    changed["revision"] += 1
    latest = ProblemVersion.objects.create(
        problem=version.problem, revision=changed["revision"], digest=digest(changed), data=changed
    )
    Problem.objects.filter(pk=version.problem_id).update(current=latest)
    card = stage_cards(revision, user)[0]
    assert card["archived"] and card["evidence"]["id"] == job.pk
    assert build_evidence(user)["summary"]["completed"] == 0
    assert build_evidence(user)["archived_attempts"] == 1


def test_science_high_chi_square_correct_solution_still_counts(catalog, user):
    version = Problem.objects.get(pk="weighted-line-fit").current
    job = job_fixture(user, version)
    assert any(
        row["model_diagnostic"] == "above_declared_scale" for row in job.result["public_cases"]
    )
    assert verified_evidence(job)["verdict"] == "accepted"


@pytest.mark.parametrize("slug", SLUGS)
def test_english_specialist_metrics_and_allowance(catalog, logged_client, user, slug):
    from django.conf import settings

    from localization.packages import import_translations, load_translations

    import_translations(load_translations(settings.CONTENT_ROOT / "en.json"))
    logged_client.cookies["django_language"] = "en"
    version = Problem.objects.get(pk=slug).current
    job = job_fixture(user, version)
    page = logged_client.get(f"/problems/{slug}/").content.decode()
    assert "Dataset identity" in page and "CPU practice allowance" in page
    page = logged_client.get(reverse("submission", args=[job.pk])).content.decode()
    assert "Only evaluator-recomputed metrics" in page
    assert ("Held-out mean squared error" if slug == SLUGS[0] else "Model diagnostics") in page


@pytest.mark.sandbox
@pytest.mark.parametrize("slug", SLUGS)
def test_real_public_api_package_drives_cli_wasi_only(catalog, client, tmp_path, slug):
    import os

    from scripts import practice

    if os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi":
        pytest.skip("Requires explicitly enabled real WASI; this is not a simulated pass")
    version = Problem.objects.get(pk=slug).current
    package = client.get(reverse("practice_api:problem", args=[slug])).json()
    downloader = practice.Client("https://example.test")
    downloader.request = lambda path: copy.deepcopy(package)
    directory = tmp_path / slug
    practice.download(downloader, slug, directory)
    (directory / "solution.py").write_text(version.data["reference"])
    result = practice.local_test(
        directory, os.environ["JUDGE_WASI_BUNDLE"], os.environ["JUDGE_WASI_SHA256"]
    )
    assert result["formal_result"] is False and result["result"]["verdict"] == "accepted"
    assert result["result"]["total"] == sum(case["public"] for case in version.data["tests"])
    assert not Submission.objects.exists()


def test_new_budgeted_result_requires_started_matching_reservation(catalog, user):
    from core.resources import BUDGET_POLICY, reservation_units, utc_day

    version = Problem.objects.get(pk=SLUGS[0]).current
    job = job_fixture(
        user,
        version,
        budget_state="consumed",
        budget_policy=BUDGET_POLICY,
        budget_day=utc_day(),
        budget_units=reservation_units(version, "submit"),
        execution_started_at=timezone.now() - timedelta(milliseconds=100),
    )
    assert verified_evidence(job)["verdict"] == "accepted"
    job.budget_units -= 1
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    job.budget_units += 1
    job.execution_started_at = None
    job.budget_state = "reserved"
    with pytest.raises(InvalidResult):
        verified_evidence(job)
