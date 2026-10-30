"""Web/queue boundaries with synthetic GPU identities, never a GPU workload."""

import io
import json
import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core import queue
from core.models import GPUDevice, QueueLock, Submission, WorkerHealth
from core.queue import configured_fingerprint
from core.resources import GPU_BUDGET_POLICY, usage
from judge.gpu_execution_contract import canonical_digest
from practice_api.services import issue_token
from tests.gpu_evidence_fixtures import environment_fixture, job_fixture, version_fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def version():
    return version_fixture()


@pytest.fixture
def cpu_ready(settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    WorkerHealth.objects.create(
        id="gpu-admission-cpu",
        ready=True,
        checked_at=timezone.now(),
        fingerprint=configured_fingerprint(),
    )


def make_device(*, number=1):
    environment = environment_fixture()
    if number != 1:
        environment["target"]["uuid"] = "GPU-" + str(uuid.UUID(int=number))
        environment["target"]["index"] = number
    return GPUDevice.objects.create(
        id=environment["target"]["uuid"],
        worker_id=f"synthetic-admission-{number}",
        owner_token=uuid.uuid4(),
        fingerprint=canonical_digest(environment),
        environment=environment,
        state="ready",
        checked_at=timezone.now(),
    )


@pytest.fixture
def gpu_ready(settings):
    device = make_device()
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_GPU_DEVICES = (device.pk,)
    return device


def occupy(device, user, version):
    job = job_fixture(user, version)
    job.status = "running"
    job.finished_at = None
    job.lease_until = timezone.now() + timedelta(seconds=180)
    job.evaluation_fingerprint = device.fingerprint
    job.save()
    device.job_id = job.pk
    device.reservation_token = job.lease_token
    device.state = "reserved"
    device.save()
    return job


def test_gpu_admission_is_independent_of_cpu_health_and_budget(gpu_ready, user, version, settings):
    settings.JUDGE_DAILY_CPU_UNITS = 1
    assert not queue.worker_available() and not queue.available()
    assert queue.supports(version) and queue.available(version)
    job = queue.enqueue(user, version, "synthetic never executed", "submit")
    assert job.evaluation_fingerprint == gpu_ready.fingerprint
    assert job.budget_policy == GPU_BUDGET_POLICY and job.budget_units == 180
    assert usage(user)["reserved"] == 0
    assert usage(user, policy=GPU_BUDGET_POLICY)["reserved"] == 180
    assert queue.claim(gpu_ready.fingerprint) is None
    assert job.status == "queued"


@pytest.mark.parametrize(
    "damage",
    [
        "disabled",
        "allowlist",
        "missing_environment",
        "old_execution",
        "wrong_target",
        "extra_field",
        "profile",
        "fingerprint",
        "stale",
        "future",
        "quarantined",
        "language",
    ],
)
def test_ineligible_device_cannot_admit_work(gpu_ready, version, user, settings, damage):
    if damage == "disabled":
        settings.JUDGE_ENABLED = False
    elif damage == "allowlist":
        settings.JUDGE_GPU_DEVICES = ()
    elif damage == "missing_environment":
        gpu_ready.environment = {}
    elif damage == "old_execution":
        gpu_ready.environment["execution_digest"] = "1" * 64
    elif damage == "wrong_target":
        gpu_ready.environment["target"]["uuid"] = "GPU-" + str(uuid.UUID(int=999))
    elif damage == "extra_field":
        gpu_ready.environment["language"] = "cuda"
    elif damage == "profile":
        gpu_ready.environment["session_profile"]["sample_batches"] = 1
    elif damage == "stale":
        gpu_ready.checked_at = timezone.now() - timedelta(seconds=31)
    elif damage == "future":
        gpu_ready.checked_at = timezone.now() + timedelta(seconds=31)
    elif damage == "quarantined":
        gpu_ready.state = "quarantined"
    elif damage == "language":
        version.data["language"] = "python"
        version.save()
    gpu_ready.fingerprint = (
        "2" * 64 if damage == "fingerprint" else canonical_digest(gpu_ready.environment)
    )
    gpu_ready.save()
    assert not queue.available(version)
    with pytest.raises(queue.AdmissionError) as error:
        queue.enqueue(user, version, "not executed", "submit")
    assert error.value.status == 503
    assert not Submission.objects.exists()


def test_pool_uses_least_pending_exact_device_and_never_retargets_existing_job(
    gpu_ready,
    version,
    user,
    settings,
):
    second = make_device(number=2)
    settings.JUDGE_GPU_DEVICES = (gpu_ready.pk, second.pk)
    first_job = queue.enqueue(user, version, "first", "submit")
    second_job = queue.enqueue(user, version, "second", "run")
    assert {first_job.evaluation_fingerprint, second_job.evaluation_fingerprint} == {
        gpu_ready.fingerprint,
        second.fingerprint,
    }
    GPUDevice.objects.filter(fingerprint=first_job.evaluation_fingerprint).update(
        state="quarantined"
    )
    first_job.refresh_from_db()
    assert first_job.evaluation_fingerprint != second_job.evaluation_fingerprint


def test_gpu_quota_and_global_pending_limit_apply(gpu_ready, version, user, settings):
    settings.JUDGE_DAILY_GPU_SECONDS = 180
    queue.enqueue(user, version, "first", "submit")
    with pytest.raises(queue.AdmissionError, match="GPU") as error:
        queue.enqueue(user, version, "second", "run")
    assert error.value.status == 429
    assert Submission.objects.count() == 1
    settings.JUDGE_DAILY_GPU_SECONDS = 1800
    settings.JUDGE_MAX_PENDING = 1
    with pytest.raises(queue.AdmissionError) as error:
        queue.enqueue(user, version, "pending limit", "run")
    assert error.value.status == 429


@pytest.mark.parametrize(
    "damage", [None, "lease", "identity", "deleted", "finished", "units", "policy"]
)
def test_reserved_device_requires_live_exact_task(gpu_ready, version, user, damage):
    job = occupy(gpu_ready, user, version)
    if damage == "lease":
        job.lease_until = timezone.now() - timedelta(seconds=1)
    elif damage == "identity":
        job.lease_token = uuid.uuid4()
    elif damage == "finished":
        job.status = "finished"
    elif damage == "units":
        job.budget_units = 1
    elif damage == "policy":
        job.budget_policy = "cpu-invocations-v1"
    job.save()
    if damage == "deleted":
        job.delete()
    assert queue.available(version) is (damage is None)


def test_pause_and_resume_classify_both_pools_and_retain_physical_slots(
    cpu_ready,
    gpu_ready,
    user,
    version,
):
    job = queue.enqueue(user, version, "synthetic", "submit")
    token = uuid.uuid4()
    queue.pause_admission(token)
    assert not queue.available(version)
    state = queue.admission_state()
    assert state["queued"] == 1 and state["incompatible_pending"] == 0
    assert state["worker_available"] and state["gpu_worker_available"]
    queue.resume_admission(token)
    assert queue.available(version)
    queue.pause_admission(token)
    job.delete()
    orphan = occupy(gpu_ready, user, version)
    orphan.delete()
    with pytest.raises(queue.AdmissionError, match="GPU worker"):
        queue.resume_admission(token)
    assert queue.admission_state()["incompatible_gpu_reservations"] == 1
    assert QueueLock.objects.get(pk=1).pause_token == token


def test_removing_pool_configuration_cannot_hide_a_physical_reservation(
    cpu_ready,
    gpu_ready,
    user,
    version,
    settings,
):
    occupy(gpu_ready, user, version).delete()
    settings.JUDGE_GPU_DEVICES = ()
    token = uuid.uuid4()
    queue.pause_admission(token)
    with pytest.raises(queue.AdmissionError, match="Unrecovered GPU"):
        queue.resume_admission(token)
    assert queue.admission_state()["gpu_reserved"] == 1


def test_http_gpu_submission_and_idempotency_keep_private_budget(
    gpu_ready,
    version,
    user,
    logged_client,
    other_user,
):
    _, raw = issue_token(user, "gpu admission", 1, can_submit=True, can_read_results=True)
    headers = {"HTTP_AUTHORIZATION": "Bearer " + raw, "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}
    payload = {"version_id": version.pk, "mode": "submit", "source": "private synthetic source"}
    url = f"/api/v1/problems/{version.problem_id}/submissions/"
    first = logged_client.post(url, payload, content_type="application/json", **headers)
    assert first.status_code == 202
    gpu_ready.state = "quarantined"
    gpu_ready.save()
    repeat = logged_client.post(url, payload, content_type="application/json", **headers)
    assert repeat.status_code == 202 and not repeat.json()["created"]
    assert repeat.json()["id"] == first.json()["id"]
    assert usage(user, policy=GPU_BUDGET_POLICY)["reserved"] == 180
    page = logged_client.get(f"/problems/{version.problem_id}/")
    assert page.status_code == 200 and "GPU 整卡时段" in page.content.decode()
    assert "CPU 练习额度" not in page.content.decode()
    assert not page.context["judge_available"]
    logged_client.force_login(other_user)
    assert logged_client.get(f"/submissions/{first.json()['id']}/").status_code == 404


def test_web_submission_uses_gpu_gate_and_requires_csrf(gpu_ready, version, user, logged_client):
    from django.test import Client

    url = f"/problems/{version.problem_id}/submit/"
    payload = {"version": version.pk, "mode": "run", "source": "synthetic"}
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(url, payload).status_code == 403
    assert logged_client.post(url, payload).status_code == 302
    job = Submission.objects.get()
    assert job.budget_policy == GPU_BUDGET_POLICY and job.budget_units == 90


def test_gpu_problem_describes_float_arrays_without_function_return_contract(
    version, logged_client
):
    response = logged_client.get(f"/problems/{version.problem_id}/")
    body = response.content.decode()
    assert "数值结果按绝对容差" in body
    assert "输入数组与形状" in body and "预期输出数组" in body
    assert "返回值的类型、结构和内容必须严格一致" not in body
    assert "使用 return 返回结果" not in body


def test_health_does_not_report_gpu_as_cpu(gpu_ready, client):
    health = client.get("/health/").json()
    assert health["evaluation"] == "unavailable" and health["gpu_evaluation"] == "available"


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_unavailable_packaged_policy_closes_gpu_without_breaking_cpu(
    cpu_ready,
    gpu_ready,
    client,
    version,
    monkeypatch,
    error,
):
    def unavailable():
        raise error("Synthetic inaccessible policy source")

    monkeypatch.setattr("core.gpu_admission.execution_digest", unavailable)
    assert not queue.available(version)
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json()["evaluation"] == "available"
    assert response.json()["gpu_evaluation"] == "unavailable"


def test_result_api_reports_both_independent_budgets(user, client, version):
    job = job_fixture(user, version)
    _, raw = issue_token(user, "synthetic gpu budget", 1, can_submit=False, can_read_results=True)
    response = client.get(f"/api/v1/submissions/{job.pk}/", HTTP_AUTHORIZATION="Bearer " + raw)
    assert response.status_code == 200
    data = response.json()
    assert data["gpu_budget"]["policy"] == GPU_BUDGET_POLICY
    assert data["gpu_budget"]["consumed"] == 180
    assert data["cpu_budget"]["consumed"] == 0


def test_drain_waits_for_physical_slot_after_finished_or_deleted_task(cpu_ready, user, version):
    job = job_fixture(user, version)
    environment = environment_fixture()
    GPUDevice.objects.create(
        id=environment["target"]["uuid"],
        worker_id="synthetic-held-slot",
        owner_token=uuid.uuid4(),
        fingerprint=job.evaluation_fingerprint,
        state="quarantined",
        job_id=job.pk,
        reservation_token=job.lease_token,
    )
    job.delete()
    output = io.StringIO()
    with pytest.raises(CommandError, match="Drain timed out"):
        call_command("judge_admission", "drain", token=uuid.uuid4(), timeout=0, stdout=output)
    assert json.loads(output.getvalue())["gpu_reserved"] == 1
