"""Actual database locks, migration and grants; GPU states remain synthetic."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from core import gpu_admission, gpu_resources, queue
from core.models import GPUDevice, Submission
from core.resources import GPU_BUDGET_POLICY, usage
from judge.gpu_device_lock import DeviceLock
from scripts.deploy_database import WEB_READ, WEB_WRITE, worker_privileges
from tests.test_gpu_admission import gpu_ready as gpu_ready
from tests.test_gpu_admission import version as version
from tests.test_gpu_readiness import startup as startup
from tests.test_gpu_resources import gpu_version as gpu_version

pytestmark = pytest.mark.django_db(transaction=True)


def require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL locks/roles")


def test_concurrent_gpu_requests_cannot_overspend(gpu_ready, version, user, settings):
    require_postgres()
    settings.JUDGE_DAILY_GPU_SECONDS = 180
    settings.JUDGE_MAX_PENDING = 10
    barrier = Barrier(2)

    def submit():
        try:
            barrier.wait(timeout=3)
            try:
                queue.enqueue(user, version, "synthetic not executed", "submit")
                return "created"
            except queue.AdmissionError as error:
                return error.status
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert set(results) == {"created", 429}
    assert Submission.objects.count() == 1
    assert usage(user, policy=GPU_BUDGET_POLICY)["reserved"] == 180


def test_web_admits_with_read_only_gpu_table_and_worker_keeps_its_grants(
    gpu_ready,
    version,
    user,
    tmp_path,
):
    require_postgres()
    names = ["px_gpu_admission_" + uuid.uuid4().hex[:16] for _ in range(2)]
    web, worker = map(connection.ops.quote_name, names)
    try:
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
            for table, privileges in worker_privileges(connection.connection).items():
                cursor.execute(
                    f"GRANT {privileges} ON {connection.ops.quote_name(table)} TO {worker}"
                )
        with DeviceLock(tmp_path / "locks", gpu_ready.pk) as guard:
            # Synthetic startup fixture, never a physical recovery assertion.
            GPUDevice.objects.filter(pk=gpu_ready.pk).update(owner_token=guard.owner_token)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL ROLE {web}")
                job = queue.enqueue(user, version, "private synthetic source", "submit")
                assert queue.available(version)
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL ROLE {web}")
                GPUDevice.objects.filter(pk=gpu_ready.pk).update(environment={})
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL ROLE {worker}")
                claimed = gpu_resources.claim(guard)
                assert claimed.pk == job.pk and gpu_resources.begin_execution(guard, claimed)
                assert gpu_resources.finish(guard, claimed, {"verdict": "internal_error"}, {}) == 1
                GPUDevice.objects.filter(pk=gpu_ready.pk).update(environment=gpu_ready.environment)
            assert GPUDevice.objects.get(pk=gpu_ready.pk).job_id == job.pk
    finally:
        with connection.cursor() as cursor:
            for role in (web, worker):
                cursor.execute(f"DROP OWNED BY {role}")
                cursor.execute(f"DROP ROLE {role}")


def test_ready_and_environment_become_visible_in_same_transaction(startup, settings, monkeypatch):
    require_postgres()
    settings.JUDGE_ENABLED = True
    settings.JUDGE_GPU_DEVICES = (startup.guard.device_uuid,)
    original = gpu_resources.confirm_recovered
    observed = []

    def read_other_connection():
        try:
            row = GPUDevice.objects.get(pk=startup.guard.device_uuid)
            return row.state, row.environment, bool(gpu_admission.candidates())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:

        def after_ready(*args, **kwargs):
            result = original(*args, **kwargs)
            observed.append(executor.submit(read_other_connection).result(timeout=3))
            return result

        monkeypatch.setattr(gpu_resources, "confirm_recovered", after_ready)
        startup.run()
    assert observed == [("quarantined", {}, False)]
    assert [device.pk for device in gpu_admission.candidates()] == [startup.guard.device_uuid]


def test_upgrade_preserves_old_slot_but_requires_full_startup_environment(settings):
    target = [("core", "0004_gpu_resources")]
    latest = [("core", "0005_gpu_admission_environment")]
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    try:
        old = executor.loader.project_state(target).apps.get_model("core", "GPUDevice")
        identifier = "GPU-" + str(uuid.UUID(int=922))
        job, token, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        old.objects.create(
            id=identifier,
            worker_id="old-controller",
            owner_token=owner,
            fingerprint="a" * 64,
            state="reserved",
            checked_at=timezone.now(),
            job_id=job,
            reservation_token=token,
        )
        MigrationExecutor(connection).migrate(latest)
        settings.JUDGE_ENABLED = True
        settings.JUDGE_GPU_DEVICES = (identifier,)
        current = GPUDevice.objects.get(pk=identifier)
        assert current.environment == {}
        assert (current.owner_token, current.job_id, current.reservation_token) == (
            owner,
            job,
            token,
        )
        assert current.state == "reserved" and not gpu_admission.candidates()
        assert queue.admission_state()["gpu_reserved"] == 1
    finally:
        MigrationExecutor(connection).migrate(latest)
