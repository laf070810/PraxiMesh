from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from core.models import ProblemVersion, Submission, WorkerHealth
from core.queue import (
    AdmissionError,
    available,
    claim,
    configured_fingerprint,
    enqueue,
    expire_jobs,
    finish,
)
from judge.evaluator import evaluate
from judge.results import matches
from judge.sandbox import DockerSandbox, Execution, SandboxUnavailable

EXACT = {"kind": "exact", "absolute": 0, "relative": 0}
NUMERIC = {"kind": "numeric", "absolute": 1e-9, "relative": 1e-9}


@pytest.mark.parametrize(
    "actual,expected,comparison,result",
    [
        (True, 1, EXACT, False),
        (1.0, 1, EXACT, False),
        (1.0, 1, NUMERIC, True),
        (float("nan"), 0, NUMERIC, False),
        (float("inf"), 1, NUMERIC, False),
        ([1], [1, 2], EXACT, False),
        ({"a": 1}, {"a": 1}, EXACT, True),
        ({"a": 1, "b": 2}, {"a": 1}, EXACT, False),
        ([0.5 + 1e-12], [0.5], NUMERIC, True),
        ([0.6], [0.5], NUMERIC, False),
        ("1", 1, EXACT, False),
        (None, None, EXACT, True),
    ],
)
def test_comparison_is_not_python_loose_equality(actual, expected, comparison, result):
    assert matches(actual, expected, comparison) is result


def test_controller_alone_assigns_pass_and_hides_cases(package):
    problem = package["problems"][0]
    sandbox = Mock()
    sandbox.execute.side_effect = [Execution("ok", c["expected"], 10) for c in problem["tests"]]
    result = evaluate(problem, "source", "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == len(problem["tests"])
    assert "全非正数" not in str(result)
    for call, case in zip(sandbox.execute.call_args_list, problem["tests"], strict=True):
        assert call.args == ("source", "solve", case["args"])


def test_wrong_return_not_accepted_and_run_uses_only_public(package):
    sandbox = Mock()
    sandbox.execute.return_value = Execution("ok", "accepted")
    assert (
        evaluate(package["problems"][0], "source", "submit", sandbox)["verdict"] == "wrong_answer"
    )
    sandbox.execute.side_effect = [
        Execution("ok", c["expected"]) for c in package["problems"][0]["tests"] if c["public"]
    ]
    sandbox.execute.reset_mock()
    result = evaluate(package["problems"][0], "source", "run", sandbox)
    assert result["total"] == sandbox.execute.call_count == 2


@pytest.mark.parametrize(
    "verdict", ["runtime_error", "syntax_error", "time_limit", "memory_limit", "output_limit"]
)
def test_error_semantics(package, verdict):
    sandbox = Mock()
    sandbox.execute.return_value = Execution(verdict)
    result = evaluate(package["problems"][0], "source", "submit", sandbox)
    assert result["verdict"] == verdict and result["passed"] == 0


def test_platform_failure_not_wrong_answer(package):
    sandbox = Mock()
    sandbox.execute.side_effect = SandboxUnavailable("daemon down")
    with pytest.raises(SandboxUnavailable):
        evaluate(package["problems"][0], "source", "submit", sandbox)


def test_queue_claim_is_unique_and_old_lease_cannot_overwrite(catalog, user, settings):
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    version = ProblemVersion.objects.first()
    queued = enqueue(user, version, "private source", "submit")
    job = claim()
    assert job.pk == queued.pk and job.status == "running"
    assert claim() is None
    Submission.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    expire_jobs()
    assert finish(job, {"verdict": "accepted"}, {}) == 0
    job.refresh_from_db()
    assert job.verdict == "internal_error"


def test_fail_closed_on_stale_heartbeat_and_policy_change(catalog, user, settings):
    settings.JUDGE_ENABLED = True
    WorkerHealth.objects.create(
        id="test",
        ready=True,
        checked_at=timezone.now() - timedelta(minutes=1),
        fingerprint=configured_fingerprint(),
    )
    assert not available()
    with pytest.raises(AdmissionError):
        enqueue(user, ProblemVersion.objects.first(), "source", "submit")
    WorkerHealth.objects.update(checked_at=timezone.now(), fingerprint="wrong")
    assert not available()


def test_user_and_global_queue_bounds(catalog, user, other_user, settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_MAX_QUEUE = 2
    WorkerHealth.objects.create(
        id="test", ready=True, checked_at=timezone.now(), fingerprint=configured_fingerprint()
    )
    version = ProblemVersion.objects.first()
    enqueue(user, version, "a", "run")
    enqueue(user, version, "b", "submit")
    for actor in (user, other_user):
        with pytest.raises(AdmissionError) as error:
            enqueue(actor, version, "c", "submit")
        assert error.value.status == 429


def test_run_never_counts_as_problem_completion(catalog, user, logged_client):
    version = ProblemVersion.objects.first()
    Submission.objects.create(
        user=user,
        version=version,
        source="source",
        source_digest="a" * 64,
        mode="run",
        status="finished",
        verdict="accepted",
        result={"label": "通过"},
    )
    assert "已通过 0 道" in logged_client.get("/me/").content.decode()


def test_sandbox_rejects_unpinned_image():
    for image in ("python:latest", "", "host", "python@sha256:bad"):
        with pytest.raises(SandboxUnavailable):
            DockerSandbox(image)


def test_sandbox_has_no_privileged_or_host_mount_fallback():
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)
    arguments = sandbox.create_args("test")
    for flag in [
        "--runtime=runsc",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--pids-limit=32",
        "--pull=never",
        "--user=65534:65534",
        "--log-driver=none",
    ]:
        assert flag in arguments
    assert not any(arg.startswith(("--privileged", "--volume", "--mount")) for arg in arguments)


def test_preflight_refuses_missing_runsc(monkeypatch):
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)
    monkeypatch.setattr(
        sandbox, "command", lambda *a, **kw: b'{"Runtimes": {"runc": {"path": "runc"}}}'
    )
    with pytest.raises(SandboxUnavailable, match="runsc"):
        sandbox.preflight()
