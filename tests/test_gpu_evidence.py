"""Persisted GPU contracts with synthetic records; no CUDA or receipt claims."""

import copy
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone, translation

from core.content import public_execution, public_problem
from core.gpu_evidence import environment_profile, public_feedback
from core.models import GPUDevice, ProblemVersion
from core.result_evidence import InvalidResult, safe_result, verified_evidence, verified_metrics
from judge import gpu_policy
from judge.gpu_execution_contract import validate_result
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable
from learning.evidence import build_evidence
from practice_api.services import issue_token
from tests.gpu_evidence_fixtures import job_fixture, rebind, version_fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def version():
    return version_fixture()


@pytest.fixture
def job(user, version):
    return job_fixture(user, version)


def test_formal_gpu_completion_is_private_and_excluded_from_cpu_boards(job, other_user):
    assert verified_evidence(job)["verdict"] == "accepted"
    with pytest.raises(InvalidResult):
        verified_metrics(job)
    assert build_evidence(job.user)["summary"]["completed"] == 1
    assert build_evidence(other_user)["summary"]["completed"] == 0
    assert not GPUDevice.objects.exists()


def test_run_is_visible_feedback_but_never_formal_evidence(user, version):
    job = job_fixture(user, version, mode="run")
    assert safe_result(job)["passed"] == 1 and "performance" not in safe_result(job)
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert build_evidence(user)["summary"]["completed"] == 0


@pytest.mark.parametrize("batch_failed", [False, True])
def test_wrong_answer_counts_as_attempt_even_after_all_correctness_cases_passed(
    user, version, batch_failed
):
    job = job_fixture(user, version, verdict="wrong_answer", batch_failed=batch_failed)
    assert verified_evidence(job)["verdict"] == "wrong_answer"
    assert "performance" not in safe_result(job)
    summary = build_evidence(user)["summary"]
    assert summary["attempts"] == 1 and summary["completed"] == 0


def test_historical_execution_hash_and_profile_survive_live_code_changes(job, monkeypatch):
    job.environment["execution_digest"] = "1" * 64
    rebind(job)
    changed = copy.deepcopy(gpu_policy.PROFILE)
    changed.update(id="synthetic-future-profile", sample_batches=7)
    monkeypatch.setattr(gpu_policy, "PROFILE", changed)
    monkeypatch.setattr(
        gpu_policy, "execution_digest", lambda: pytest.fail("History must not hash live code")
    )
    assert verified_evidence(job)["assessment"]["resource_profile"] == "cuda-session-v1"
    assert safe_result(job)["performance"]["sample_batches"] == 5


@pytest.mark.parametrize(
    "damage",
    [
        "fingerprint",
        "policy",
        "reserved",
        "budget",
        "lease",
        "source",
        "order",
        "unfinished",
        "verdict",
    ],
)
def test_incomplete_task_or_unconsumed_gpu_reservation_is_not_evidence(job, damage):
    if damage == "fingerprint":
        job.evaluation_fingerprint = "f" * 64
    elif damage == "policy":
        job.budget_policy = "cpu-invocations-v1"
    elif damage == "reserved":
        job.budget_state = "reserved"
    elif damage == "budget":
        job.budget_units = True
    elif damage == "lease":
        job.lease_token = None
    elif damage == "source":
        job.source_digest = "missing"
    elif damage == "order":
        job.execution_started_at = job.finished_at + timedelta(seconds=1)
    elif damage == "unfinished":
        job.status = "running"
    else:
        job.verdict = "internal_error"
    with pytest.raises(InvalidResult):
        verified_evidence(job)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "backend",
        "protocol",
        "target",
        "profile",
        "profile_digest",
        "artifacts",
        "library",
        "harness",
        "limits",
        "margin",
    ],
)
def test_environment_corruption_is_rejected_even_with_recomputed_fingerprint(job, damage):
    env = job.environment
    if damage == "missing":
        env.pop("artifacts")
    elif damage == "extra":
        env["hidden"] = "PRIVATE_ENVIRONMENT_SENTINEL"
    elif damage == "backend":
        env["backend"] = "ordinary-docker"
    elif damage == "protocol":
        env["controller_protocol"] = True
    elif damage == "target":
        env["target"]["index"] = True
    elif damage == "profile":
        env["session_profile"]["sample_batches"] = 4
    elif damage == "profile_digest":
        env["profile_digest"] = "f" * 64
    elif damage == "artifacts":
        env["artifacts"]["runsc_sha256"] = "not-pinned"
    elif damage == "library":
        env["artifacts"]["nvrtc_libraries"] = {
            "/private-path": "a" * 64,
            "libnvrtc.so.13.1.115": "b" * 64,
        }
    elif damage == "harness":
        env["artifacts"]["harness_sha256"]["unreviewed.py"] = "a" * 64
    elif damage == "limits":
        env["workload_limits"]["cpu.max"] = "max 100000"
    else:
        env["recovery_margin_seconds"] = True
    rebind(job)
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert safe_result(job) == {}


@pytest.mark.parametrize(
    "damage",
    [
        "hidden",
        "extra",
        "nested",
        "count",
        "bool",
        "nan",
        "median",
        "missing_sample",
        "zero",
        "no_compile",
        "compile_bool",
        "profile",
        "measurement",
    ],
)
def test_invalid_feedback_cannot_escape_or_count_as_completion(job, damage):
    result = job.result
    if damage == "hidden":
        result["public_cases"][0]["name"] = "PROTECTED_CASE_SENTINEL"
    elif damage == "extra":
        result["stdout"] = "PRIVATE_LOG_SENTINEL"
    elif damage == "nested":
        result["performance"]["source"] = "PRIVATE_SOURCE_SENTINEL"
    elif damage == "count":
        result["total"] = 1
    elif damage == "bool":
        result["performance"]["samples_ms"][0] = True
    elif damage == "nan":
        result["performance"]["samples_ms"][0] = float("nan")
    elif damage == "median":
        result["performance"]["median_batch_ms"] = 1.0
    elif damage == "missing_sample":
        result["performance"]["samples_ms"].pop()
    elif damage == "zero":
        result["performance"]["samples_ms"][0] = 0.0
    elif damage == "no_compile":
        result.pop("compile_initialization_ms")
    elif damage == "compile_bool":
        result["compile_initialization_ms"] = True
    elif damage == "profile":
        result["performance"]["profile"] = "unknown"
    else:
        result["performance"]["measurement"] = "guest-reported-kernel-time"
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert safe_result(job) == {}


@pytest.mark.parametrize("mode,verdict", [("run", "accepted"), ("submit", "wrong_answer")])
def test_only_accepted_submit_can_carry_any_performance_field(user, version, mode, verdict):
    job = job_fixture(user, version, mode=mode, verdict=verdict)
    job.result["performance"] = None
    assert safe_result(job) == {}


def test_public_content_projection_excludes_hidden_cases_reference_and_learner_code(version):
    for data in (public_execution(version), public_problem(version)):
        rendered = repr(data)
        assert (
            "PRIVATE_REFERENCE_SENTINEL" not in rendered
            and "PROTECTED_CASE_SENTINEL" not in rendered
        )
        assert data["operation"] == "vector-add-f32-v1" and data["entrypoint"] == "solve"


def test_result_projection_does_not_share_mutable_persisted_objects(job):
    feedback = safe_result(job)
    feedback["performance"]["samples_ms"].clear()
    assert len(job.result["performance"]["samples_ms"]) == 5
    assert (
        environment_profile(job.environment, job.evaluation_fingerprint)["id"] == "cuda-session-v1"
    )


def test_result_page_uses_validated_counts_and_gpu_units(job, logged_client, other_user):
    url = reverse("submission", args=[job.pk])
    response = logged_client.get(url)
    text = response.content.decode()
    assert response.status_code == 200
    assert "控制器批次往返时间中位数" in text and "12.500" in text and "3.000" in text
    assert "180 秒 GPU 整卡时段" in text and "180 个 CPU" not in text
    assert "总等待" not in text and "选择公开这条评测结果" not in text
    assert "PRIVATE_REFERENCE_SENTINEL" not in text and "PROTECTED_CASE_SENTINEL" not in text
    job.result["total"] = "PROTECTED_COUNT_SENTINEL"
    job.save(update_fields=["result"])
    text = logged_client.get(url).content.decode()
    assert "PROTECTED_COUNT_SENTINEL" not in text and "控制器批次往返时间中位数" not in text
    logged_client.force_login(other_user)
    assert logged_client.get(url).status_code == 404


def test_api_and_export_preserve_private_gpu_result_without_hidden_data(
    job, logged_client, client, other_user
):
    token, secret = issue_token(job.user, "GPU fixture", 1, can_submit=False, can_read_results=True)
    response = client.get(
        reverse("practice_api:result", args=[job.pk]), HTTP_AUTHORIZATION="Bearer " + secret
    )
    data = response.json()
    assert data["formal_result"] is True and data["result"]["performance"]["median_batch_ms"] == 3.0
    assert "CUDA / Synthetic device" in data["evaluation_environment"]
    assert "PRIVATE_LEARNER_SOURCE_SENTINEL" not in response.content.decode()
    other, other_secret = issue_token(
        other_user, "Other fixture", 1, can_submit=False, can_read_results=True
    )
    assert (
        client.get(
            reverse("practice_api:result", args=[job.pk]),
            HTTP_AUTHORIZATION="Bearer " + other_secret,
        ).status_code
        == 404
    )
    exported = logged_client.post(reverse("export_account")).json()
    assert exported["submissions"][0]["result"]["performance"]["median_batch_ms"] == 3.0
    assert "PROTECTED_CASE_SENTINEL" not in repr(
        exported
    ) and "PRIVATE_REFERENCE_SENTINEL" not in repr(exported)


def test_english_gpu_result_page_has_translated_units_and_timing(job, logged_client):
    with translation.override("en"):
        response = logged_client.get(
            reverse("submission", args=[job.pk]), HTTP_ACCEPT_LANGUAGE="en"
        )
    text = response.content.decode()
    assert "Controller round-trip batch median" in text and "GPU device-slot seconds" in text


def test_compilation_failure_is_an_attempt_without_a_performance_score(user, version):
    job = job_fixture(user, version, verdict="syntax_error")
    job.result = {
        "verdict": "syntax_error",
        "label": LABELS["syntax_error"],
        "passed": 0,
        "total": 2,
        "public_cases": [],
    }
    assert verified_evidence(job)["verdict"] == "syntax_error"
    assert public_feedback(version, job.result, "submit") == job.result


@pytest.mark.parametrize(
    "damage", ["fewer_passes_than_public", "too_many_passes", "missing_public"]
)
def test_contradictory_public_counts_are_not_evidence(user, version, damage):
    job = job_fixture(user, version, verdict="wrong_answer")
    if damage == "fewer_passes_than_public":
        job.result["passed"] = 0
    elif damage == "too_many_passes":
        job.result["passed"] = 2
        job.result["public_cases"][0].update(verdict="wrong_answer", label=LABELS["wrong_answer"])
    else:
        job.result["public_cases"] = []
    with pytest.raises(InvalidResult):
        verified_evidence(job)
    assert safe_result(job) == {}


@pytest.mark.parametrize("damage", ["compile_zero", "compile_missing", "sample_zero"])
def test_ipc_rejects_incomplete_success_before_worker_settlement(job, damage):
    if damage == "compile_zero":
        job.result["compile_initialization_ms"] = 0
    elif damage == "compile_missing":
        job.result.pop("compile_initialization_ms")
    else:
        job.result["performance"]["samples_ms"][0] = 0
    with pytest.raises(SandboxUnavailable):
        validate_result(job.result, job.version.data, job.mode)


def test_pinned_course_and_challenge_require_owned_valid_gpu_evidence(job, other_user):
    from challenges.models import Challenge, ChallengeRevision, ChallengeTask, Participation
    from challenges.progress import task_cards
    from organizations.models import Course, CourseRevision, CourseUnit, Organization
    from organizations.progress import own_progress

    organization = Organization.objects.create(
        id="gpu-fixture", name="Synthetic course", created_by=job.user
    )
    course = Course.objects.create(organization=organization, slug="gpu-fixture")
    revision = CourseRevision.objects.create(
        course=course, revision=1, data={}, digest="d" * 64, author=job.user
    )
    CourseUnit.objects.create(
        revision=revision,
        key="kernel",
        position=0,
        title="GPU fixture",
        description="Synthetic evidence only",
        problem_version=job.version,
    )
    challenge = Challenge.objects.create(id="gpu-fixture")
    activity = ChallengeRevision.objects.create(
        challenge=challenge,
        revision=1,
        digest="e" * 64,
        data={},
        starts_at=job.created_at - timedelta(days=1),
        ends_at=timezone.now() + timedelta(days=1),
    )
    ChallengeTask.objects.create(
        revision=activity,
        key="kernel",
        position=0,
        problem_version=job.version,
        data={"title": "GPU fixture", "description": "Synthetic evidence only"},
    )
    member = Participation.objects.create(user=job.user, revision=activity)
    Participation.objects.filter(pk=member.pk).update(
        joined_at=job.created_at - timedelta(seconds=1)
    )
    member.refresh_from_db()
    assert own_progress(revision, job.user)[0]["evidence"]["id"] == job.pk
    assert task_cards(activity, member)[0]["evidence"]["id"] == job.pk
    assert own_progress(revision, other_user)[0]["evidence"] is None
    other_member = Participation.objects.create(user=other_user, revision=activity)
    assert task_cards(activity, other_member)[0]["evidence"] is None
    # A new content version does not inherit the old score. Pinned consumers
    # still recognize their original immutable version and identify it as old.
    data = copy.deepcopy(job.version.data)
    data["revision"] = 2
    replacement = ProblemVersion.objects.create(
        problem=job.version.problem, revision=2, data=data, digest="f" * 64
    )
    job.version.problem.current = replacement
    job.version.problem.save(update_fields=["current"])
    progress = own_progress(revision, job.user)[0]
    assert progress["archived"] and progress["evidence"]["id"] == job.pk
    assert build_evidence(job.user)["summary"]["completed"] == 0
    job.evaluation_fingerprint = "0" * 64
    job.save(update_fields=["evaluation_fingerprint"])
    assert own_progress(revision, job.user)[0]["evidence"] is None
    assert task_cards(activity, member)[0]["evidence"] is None
