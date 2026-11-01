"""Real PostgreSQL row/advisory locks; no daemon, GPU, or deployment activation.

Connections are mapped to the test runner's disposable database. Database owner
and role creation are outside these tests; normal publication uses connection().
"""

import uuid
from contextlib import contextmanager

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from core.models import GPUDevice, QueueLock
from scripts.deploy_database import DeploymentDatabase, DeploymentDatabaseError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def guarded_pg():
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL publication row/advisory locks")
    database = object.__new__(DeploymentDatabase)
    database._operation_connection = None
    database._lock_key = uuid.uuid4().int % (2**63)
    parameters = connection.get_connection_params()

    def connect(name, *, autocommit=False):
        return psycopg.connect(**parameters, autocommit=autocommit)

    @contextmanager
    def original(name):
        assert name == "owned-test-database"
        with connect(name) as session:
            yield session

    database._connect, database.connection = connect, original
    token = uuid.uuid4()
    QueueLock.objects.update_or_create(
        pk=1, defaults={"pause_token": token, "paused_at": timezone.now()}
    )
    device = GPUDevice.objects.create(
        id="GPU-" + str(uuid.uuid4()),
        worker_id="synthetic-unused",
        owner_token=uuid.uuid4(),
        fingerprint="a" * 64,
    )
    return database, device, str(token)


def test_publication_guard_holds_device_queue_and_operation_locks_until_exit(guarded_pg):
    database, device, token = guarded_pg
    with database.operation_lock():
        with database.gpu_configuration_guard(["owned-test-database"], token) as recheck:
            recheck()
            with database._connect("probe", autocommit=True) as probe:
                assert probe.execute(
                    "SELECT pg_try_advisory_lock(%s)", (database._lock_key,)
                ).fetchone() == (False,)
                probe.execute("SET lock_timeout = '150ms'")
                for statement, params in [
                    (
                        "INSERT INTO core_gpudevice "
                        "(id, state, worker_id, owner_token, fingerprint, environment) "
                        "VALUES (%s, 'quarantined', 'synthetic-unused', %s, %s, '{}'::jsonb)",
                        ("GPU-" + str(uuid.uuid4()), uuid.uuid4(), "b" * 64),
                    ),
                    ("UPDATE core_gpudevice SET checked_at=NULL WHERE id=%s", (device.pk,)),
                    ("UPDATE core_queuelock SET pause_token=NULL, paused_at=NULL WHERE id=1", ()),
                ]:
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        probe.execute(statement, params)
        with database._connect("probe", autocommit=True) as probe:
            probe.execute("SET lock_timeout = '150ms'")
            assert (
                probe.execute(
                    "UPDATE core_gpudevice SET checked_at=NULL WHERE id=%s", (device.pk,)
                ).rowcount
                == 1
            )
    with database._connect("probe", autocommit=True) as probe:
        assert probe.execute(
            "SELECT pg_try_advisory_lock(%s)", (database._lock_key,)
        ).fetchone() == (True,)


def test_deleted_task_reservation_and_wrong_pause_cannot_publish(guarded_pg):
    database, device, token = guarded_pg
    # No Submission row exists for this original physical reservation.
    GPUDevice.objects.filter(pk=device.pk).update(
        job_id=uuid.uuid4(), reservation_token=uuid.uuid4()
    )
    with database.operation_lock():
        with pytest.raises(DeploymentDatabaseError, match="fully drained"):
            with database.gpu_configuration_guard(["owned-test-database"], token):
                pytest.fail("Orphan physical reservation accepted")
        GPUDevice.objects.filter(pk=device.pk).update(job_id=None, reservation_token=None)
        QueueLock.objects.filter(pk=1).update(pause_token=uuid.uuid4())
        with pytest.raises(DeploymentDatabaseError, match="fully drained"):
            with database.gpu_configuration_guard(["owned-test-database"], token):
                pytest.fail("Another operation's pause accepted")


def test_lost_live_operation_lock_invalidates_held_publication_guard(guarded_pg):
    database, _, token = guarded_pg
    with database.operation_lock():
        with pytest.raises(DeploymentDatabaseError, match="lock was lost"):
            with database.gpu_configuration_guard(["owned-test-database"], token) as recheck:
                database._operation_connection.execute(
                    "SELECT pg_advisory_unlock(%s)", (database._lock_key,)
                )
                recheck()


def test_worker_pause_allows_original_reservation_and_releases_its_connection(guarded_pg):
    database, device, token = guarded_pg
    job, reservation = uuid.uuid4(), uuid.uuid4()
    GPUDevice.objects.filter(pk=device.pk).update(job_id=job, reservation_token=reservation)
    with database.operation_lock():
        database.gpu_worker_pause("owned-test-database", token)
        with database._connect("probe", autocommit=True) as probe:
            probe.execute("SET lock_timeout = '150ms'")
            assert (
                probe.execute("UPDATE core_queuelock SET paused_at=paused_at WHERE id=1").rowcount
                == 1
            )
    device.refresh_from_db()
    assert device.job_id == job and device.reservation_token == reservation


def test_readiness_snapshot_keeps_original_pause_and_observes_other_device_reservation(guarded_pg):
    database, device, token = guarded_pg
    other = GPUDevice.objects.create(
        id="GPU-" + str(uuid.uuid4()),
        worker_id="other-original-worker",
        owner_token=uuid.uuid4(),
        fingerprint="b" * 64,
        job_id=uuid.uuid4(),
        reservation_token=uuid.uuid4(),
    )
    with database.operation_lock():
        result = database.gpu_worker_snapshot("owned-test-database", token, device.pk)
    assert result["device"]["id"] == device.pk
    assert result["device"]["owner_token"] == device.owner_token
    assert result["reservations"] is True and result["pending"] is False
    assert result["observed_at"] <= timezone.now()
    other.refresh_from_db()
    assert other.job_id and other.reservation_token


def test_readiness_snapshot_is_read_only_and_does_not_block_worker_heartbeat(
    guarded_pg, monkeypatch
):
    from scripts import deploy_database

    database, device, token = guarded_pg
    original = deploy_database.worker_privileges

    def check(session):
        assert session.execute("SHOW transaction_read_only").fetchone() == ("on",)
        with database._connect("probe", autocommit=True) as probe:
            probe.execute("SET lock_timeout = '150ms'")
            assert (
                probe.execute(
                    "UPDATE core_gpudevice SET checked_at=NULL WHERE id=%s", (device.pk,)
                ).rowcount
                == 1
            )
            assert (
                probe.execute("UPDATE core_queuelock SET paused_at=paused_at WHERE id=1").rowcount
                == 1
            )
        return original(session)

    monkeypatch.setattr(deploy_database, "worker_privileges", check)
    with database.operation_lock():
        result = database.gpu_worker_snapshot("owned-test-database", token, device.pk)
    assert result["device"]["checked_at"] is None
