"""Persistent device fences and budgets, without opening real GPU admission."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.utils import timezone

from core import gpu_resources, queue
from core.models import GPUDevice, Problem, ProblemVersion, Submission, WorkerHealth
from core.resources import (
    BUDGET_POLICY,
    GPU_BUDGET_POLICY,
    reservation_units,
    usage,
    utc_day,
)
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from scripts.deploy_database import WEB_READ, WEB_WRITE, worker_privileges

DEVICE = "GPU-11111111-2222-3333-4444-555555555555"
FINGERPRINT = "e" * 64


@pytest.fixture
def gpu_version(db):
    problem = Problem.objects.create(id="gpu-scheduling-fixture")
    version = ProblemVersion.objects.create(
        problem=problem,
        revision=1,
        digest="d" * 64,
        data={
            "evaluator": "cuda-kernel-v1",
            "language": "cuda",
            "entrypoint": "solve",
            "operation": "vector-add-f32-v1",
            "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
            "tests": [
                {"name": "public", "public": True, "inputs": {"a": [1], "b": [2]}, "expected": [3]},
                {
                    "name": "private",
                    "public": False,
                    "inputs": {"a": [2], "b": [4]},
                    "expected": [6],
                },
            ],
        },
    )
    problem.current = version
    problem.save(update_fields=["current"])
    return version


@pytest.fixture
def guard(tmp_path, db):
    with DeviceLock(tmp_path / "locks", DEVICE) as value:
        gpu_resources.adopt(value, "scheduler-test", FINGERPRINT)
        # Only the DB transition is under test. Production callers must first
        # perform real device recovery; no GPU receipt is created by these tests.
        gpu_resources.confirm_recovered(value, job_id=None, reservation_token=None)
        yield value


def queued(user, version, *, mode="submit", **overrides):
    values = {
        "user": user,
        "version": version,
        "mode": mode,
        "source": "Synthetic scheduling record; never compiled or executed",
        "source_digest": "a" * 64,
        "evaluation_fingerprint": FINGERPRINT,
        "budget_policy": GPU_BUDGET_POLICY,
        "budget_state": "reserved",
        "budget_units": reservation_units(version, mode),
        "budget_day": utc_day(),
    }
    return Submission.objects.create(**(values | overrides))


def recover(guard, job):
    gpu_resources.confirm_recovered(guard, job_id=job.id, reservation_token=job.lease_token)


def test_device_claim_is_exclusive_and_cpu_worker_cannot_take_gpu_work(guard, gpu_version, user):
    first = queued(user, gpu_version)
    second = queued(user, gpu_version)
    assert queue.claim(FINGERPRINT) is None
    claimed = gpu_resources.claim(guard)
    assert claimed.pk == first.pk
    assert gpu_resources.claim(guard) is None
    assert gpu_resources.available(DEVICE, FINGERPRINT)
    assert not queue.begin_execution(claimed) and not queue.renew(claimed)
    assert queue.finish(claimed, {"verdict": "internal_error"}, {}) == 0
    second.refresh_from_db()
    assert second.status == "queued"


def test_gpu_admission_remains_closed_even_with_a_cpu_worker(gpu_version, user, settings):
    settings.JUDGE_ENABLED = True
    settings.JUDGE_BACKEND = "wasi"
    WorkerHealth.objects.create(
        id="cpu", ready=True, checked_at=timezone.now(), fingerprint=queue.configured_fingerprint()
    )
    assert not queue.available(gpu_version)
    with pytest.raises(queue.AdmissionError):
        queue.enqueue(user, gpu_version, "not executed", "submit")


def test_gpu_and_cpu_budgets_do_not_mix_and_cost_cannot_be_supplied_by_content(gpu_version, user):
    gpu_version.data["reservation_units"] = 0
    assert reservation_units(gpu_version, "run") == 90
    assert reservation_units(gpu_version, "submit") == 180
    queued(user, gpu_version)
    queued(user, gpu_version, budget_policy=BUDGET_POLICY, budget_units=2)
    assert usage(user)["reserved"] == 2
    assert usage(user, policy=GPU_BUDGET_POLICY)["reserved"] == 180
    gpu_version.data["tests"][0]["public"] = 1
    with pytest.raises(ValueError):
        reservation_units(gpu_version, "run")


def test_finishing_does_not_release_device_before_recovery(guard, gpu_version, user):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    assert gpu_resources.renew(guard, job)
    job.refresh_from_db()
    assert job.budget_state == "reserved"
    assert gpu_resources.begin_execution(guard, job)
    assert gpu_resources.begin_execution(guard, job)
    assert gpu_resources.finish(guard, job, {"verdict": "internal_error"}, {}) == 1
    job.refresh_from_db()
    device = GPUDevice.objects.get(pk=DEVICE)
    assert job.budget_state == "consumed" and job.execution_started_at
    assert device.state == "quarantined" and device.job_id == job.id
    assert not gpu_resources.available(DEVICE, FINGERPRINT)
    assert not gpu_resources.heartbeat(guard)
    recover(guard, job)
    device.refresh_from_db()
    assert device.state == "ready" and device.job_id is None
    assert usage(user, policy=GPU_BUDGET_POLICY)["consumed"] == 180


@pytest.mark.parametrize("started", [False, True])
def test_expiry_never_frees_device_and_recovery_never_refunds_started_work(
    guard, gpu_version, user, started
):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    if started:
        assert gpu_resources.begin_execution(guard, job)
    Submission.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    queue.expire_jobs()
    assert GPUDevice.objects.get(pk=DEVICE).job_id == job.id
    assert gpu_resources.claim(guard) is None
    assert not gpu_resources.available(DEVICE, FINGERPRINT)
    assert not gpu_resources.begin_execution(guard, job)
    assert gpu_resources.finish(guard, job, {"verdict": "internal_error"}, {}) == 0
    recover(guard, job)
    job.refresh_from_db()
    assert job.budget_state == ("consumed" if started else "released")


def test_user_deletion_keeps_operational_reservation_until_physical_recovery(
    guard, gpu_version, user, other_user, logged_client
):
    own = queued(user, gpu_version)
    foreign = queued(other_user, gpu_version)
    job = gpu_resources.claim(guard)
    assert job.pk == own.pk and gpu_resources.begin_execution(guard, job)
    exported = logged_client.post("/accounts/export/").json()
    assert exported["gpu_budget"]["consumed"] == 180
    assert {row["id"] for row in exported["submissions"]} == {str(own.pk)}
    response = logged_client.post(
        "/accounts/delete/", {"password": "Test-passphrase-9621", "confirm": "DELETE"}
    )
    assert response.status_code == 200
    assert not Submission.objects.filter(pk=own.pk).exists()
    assert Submission.objects.filter(pk=foreign.pk).exists()
    assert GPUDevice.objects.get(pk=DEVICE).job_id == own.id
    assert not gpu_resources.renew(guard, job)
    recover(guard, job)
    assert gpu_resources.claim(guard).pk == foreign.pk


def test_wrong_recovery_token_cannot_release_current_or_next_job(guard, gpu_version, user):
    queued(user, gpu_version)
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        recover(guard, job)
    gpu_resources.quarantine(guard, job)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        gpu_resources.confirm_recovered(guard, job_id=job.id, reservation_token=uuid.uuid4())
    recover(guard, job)
    current = gpu_resources.claim(guard)
    assert current.pk != job.pk
    assert not gpu_resources.quarantine(guard, job)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        recover(guard, job)
    assert GPUDevice.objects.get(pk=DEVICE).job_id == current.id


def test_new_host_owner_retains_old_reservation_and_stale_owner_cannot_finish(
    guard, gpu_version, user
):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    assert gpu_resources.begin_execution(guard, job)
    old_token = guard.owner_token
    guard.close()
    with DeviceLock(guard.directory, DEVICE) as replacement:
        device = gpu_resources.adopt(replacement, "replacement", FINGERPRINT)
        assert device.owner_token != old_token
        assert device.state == "quarantined" and device.job_id == job.id
        assert gpu_resources.claim(replacement) is None
        with pytest.raises(DeviceLockError):
            gpu_resources.finish(guard, job, {"verdict": "internal_error"}, {})
        recover(replacement, job)
    job.refresh_from_db()
    assert job.verdict == "internal_error" and job.budget_state == "consumed"


def test_stale_heartbeat_wrong_fingerprint_and_fake_guard_do_not_authorize_work(
    guard, gpu_version, user
):
    queued(user, gpu_version, evaluation_fingerprint="f" * 64)
    assert gpu_resources.claim(guard) is None
    assert not gpu_resources.available(DEVICE, "f" * 64)
    GPUDevice.objects.filter(pk=DEVICE).update(checked_at=timezone.now() - timedelta(seconds=31))
    assert gpu_resources.claim(guard) is None
    assert not gpu_resources.available(DEVICE, FINGERPRINT)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        gpu_resources.adopt(SimpleNamespace(assert_held=lambda: None), "fake", FINGERPRINT)


def test_database_rejects_ready_device_with_active_reservation_and_duplicate_task(
    guard, gpu_version, user
):
    queued(user, gpu_version)
    job = gpu_resources.claim(guard)
    with pytest.raises(IntegrityError), transaction.atomic():
        GPUDevice.objects.filter(pk=DEVICE).update(state="ready")
    with pytest.raises(IntegrityError), transaction.atomic():
        GPUDevice.objects.create(
            id="GPU-99999999-2222-3333-4444-555555555555",
            worker_id="other",
            owner_token=uuid.uuid4(),
            fingerprint=FINGERPRINT,
            job_id=job.id,
            reservation_token=uuid.uuid4(),
        )


@pytest.mark.django_db(transaction=True)
def test_competing_claims_get_only_one_physical_slot(guard, gpu_version, user):
    if connection.vendor != "postgresql":
        pytest.skip("Actual PostgreSQL row locks are required")
    first = queued(user, gpu_version)
    queued(user, gpu_version)

    def attempt(_):
        try:
            job = gpu_resources.claim(guard)
            return job.id if job else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count(first.pk) == 1 and results.count(None) == 3


@pytest.mark.django_db(transaction=True)
def test_minimal_roles_can_schedule_but_web_cannot_release_device(guard, gpu_version, user):
    if connection.vendor != "postgresql":
        pytest.skip("Production PostgreSQL role permissions")
    names = ["px_gpu_test_" + uuid.uuid4().hex[:16] for _ in range(2)]
    web, worker = map(connection.ops.quote_name, names)
    try:
        grants = worker_privileges(connection.connection)
        with connection.cursor() as cursor:
            for role in (web, worker):
                cursor.execute(f"CREATE ROLE {role} NOLOGIN")
                cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            for table in WEB_READ:
                cursor.execute(f"GRANT SELECT ON {connection.ops.quote_name(table)} TO {web}")
            for table in WEB_WRITE:
                cursor.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {connection.ops.quote_name(table)} TO {web}"
                )
            cursor.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {web}")
            for table, permissions in grants.items():
                cursor.execute(
                    f"GRANT {permissions} ON {connection.ops.quote_name(table)} TO {worker}"
                )
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {web}")
            waiting = queued(user, gpu_version)
            assert gpu_resources.available(DEVICE, FINGERPRINT)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {worker}")
            job = gpu_resources.claim(guard)
            assert job.pk == waiting.pk
            assert gpu_resources.begin_execution(guard, job)
            assert gpu_resources.renew(guard, job)
            assert gpu_resources.finish(guard, job, {"verdict": "internal_error"}, {}) == 1
        for role in (web, worker):
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL ROLE {role}")
                GPUDevice.objects.filter(pk=DEVICE).delete()
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {web}")
            GPUDevice.objects.filter(pk=DEVICE).update(state="quarantined")
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {worker}")
            ProblemVersion.objects.filter(pk=gpu_version.pk).update(data={})
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {web}")
            user.delete()
        assert GPUDevice.objects.get(pk=DEVICE).job_id == job.pk
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {worker}")
            recover(guard, job)
            assert gpu_resources.heartbeat(guard)
    finally:
        with connection.cursor() as cursor:
            for role in (web, worker):
                cursor.execute(f"DROP OWNED BY {role}")
                cursor.execute(f"DROP ROLE {role}")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["begin_execution", "renew", "finish"])
@pytest.mark.parametrize("pool", ["gpu", "cpu"])
def test_lease_expiring_while_waiting_for_task_lock_cannot_be_revived(
    guard, gpu_version, user, monkeypatch, operation, pool
):
    if connection.vendor != "postgresql":
        pytest.skip("Actual PostgreSQL lock wait across a lease deadline")
    if pool == "gpu":
        queued(user, gpu_version)
        job = gpu_resources.claim(guard)
    else:
        version = ProblemVersion.objects.create(
            problem=Problem.objects.create(id="cpu-lock-fixture"),
            revision=1,
            digest="c" * 64,
            data={
                "evaluator": "python-function-v1",
                "language": "python",
                "tests": [{"public": True}, {"public": False}],
            },
        )
        queued(user, version, budget_policy=BUDGET_POLICY)
        job = queue.claim(FINGERPRINT)
    before = timezone.now()
    after = job.lease_until + timedelta(seconds=1)
    clock = [before]
    monkeypatch.setattr(gpu_resources.timezone, "now", lambda: clock[0])
    connected = Event()
    backend_pid = []

    def attempt():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid.append(cursor.fetchone()[0])
            connected.set()
            function = getattr(gpu_resources if pool == "gpu" else queue, operation)
            args = ({"verdict": "internal_error"}, {}) if operation == "finish" else ()
            return function(guard, job, *args) if pool == "gpu" else function(job, *args)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Submission.objects.select_for_update().get(pk=job.pk)
            pending = executor.submit(attempt)
            assert connected.wait(timeout=3)
            deadline = time.monotonic() + 3
            while True:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_stat_clear_snapshot()")
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        [backend_pid[0]],
                    )
                    waiting = cursor.fetchone()[0]
                if waiting == "Lock":
                    break
                assert time.monotonic() < deadline, "Worker never reached the actual row lock"
                time.sleep(0.01)
            clock[0] = after
        assert not pending.result(timeout=3)
    job.refresh_from_db()
    device = GPUDevice.objects.get(pk=DEVICE)
    assert job.status == "running" and job.budget_state == "reserved"
    assert job.lease_until < after and job.execution_started_at is None
    if pool == "gpu":
        assert device.state == "quarantined" and device.job_id == job.pk
    else:
        assert device.state == "ready" and device.job_id is None
