"""SQL boundaries with explicit substitutes; real PostgreSQL cases are separate."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from scripts import deploy_database
from scripts.deploy_database import DeploymentDatabaseError
from tests.test_deploy_database import configuration as configuration
from tests.test_deploy_database import database as database


@pytest.fixture
def snapshot_db(database, monkeypatch):
    token, device = str(uuid.uuid4()), "GPU-" + str(uuid.uuid4())
    now = datetime.now(UTC)
    value = SimpleNamespace(
        token=token,
        device=device,
        name=database.name("aaaaaaaaaaaa"),
        calls=[],
        opened=0,
        closed=0,
        lock=True,
        schema=True,
        row=(
            now,
            uuid.UUID(token),
            now,
            device,
            "ready",
            "worker",
            uuid.uuid4(),
            "a" * 64,
            {},
            now,
            None,
            None,
            False,
            False,
        ),
    )

    def lock():
        if not value.lock:
            raise DeploymentDatabaseError("Original operation lock lost")

    class Connection:
        def execute(self, sql, params=None):
            value.calls.append((sql, params))
            if sql == "SET TRANSACTION READ ONLY":
                return None
            assert sql.startswith("SELECT statement_timestamp(),")
            assert params == (device,)
            return SimpleNamespace(fetchone=lambda: value.row)

    @contextmanager
    def connect(name):
        assert name == value.name
        value.opened += 1
        try:
            yield Connection()
        finally:
            value.closed += 1

    monkeypatch.setattr(database, "_require_operation_lock", lock)
    monkeypatch.setattr(database, "connection", connect)
    monkeypatch.setattr(
        deploy_database,
        "worker_privileges",
        lambda conn: {"core_gpudevice": "recorded"} if value.schema else {},
    )
    value.read = lambda: database.gpu_worker_snapshot(value.name, token, device)
    return value


def test_one_read_only_statement_observes_pause_target_and_all_reservations(snapshot_db):
    value = snapshot_db
    result = value.read()
    assert result["device"]["id"] == value.device
    assert result["observed_at"] == value.row[0]
    assert value.opened == value.closed == 1
    assert len(value.calls) == 2
    sql = value.calls[1][0]
    assert "FOR UPDATE" not in sql and "LOCK TABLE" not in sql
    assert "WHERE job_id IS NOT NULL OR reservation_token IS NOT NULL" in sql
    assert "WHERE status IN ('queued', 'running')" in sql


@pytest.mark.parametrize("offset", [12, 13])
def test_pending_or_orphan_work_is_observed_without_clearing_it(snapshot_db, offset):
    value = snapshot_db
    row = list(value.row)
    row[offset] = True
    value.row = tuple(row)
    result = value.read()
    assert result["pending" if offset == 12 else "reservations"] is True
    assert value.row == tuple(row) and value.closed == 1


def test_missing_device_is_not_fabricated(snapshot_db):
    value = snapshot_db
    value.row = (*value.row[:3], *([None] * 9), False, False)
    assert value.read()["device"] is None


@pytest.mark.parametrize("defect", ["missing-pause", "wrong-token", "unpaused", "schema", "lock"])
def test_snapshot_requires_original_pause_schema_and_live_lock(snapshot_db, defect):
    value = snapshot_db
    if defect == "missing-pause":
        value.row = None
    elif defect in {"wrong-token", "unpaused"}:
        row = list(value.row)
        row[1 if defect == "wrong-token" else 2] = uuid.uuid4() if defect == "wrong-token" else None
        value.row = tuple(row)
    elif defect == "schema":
        value.schema = False
    else:
        value.lock = False
    with pytest.raises(DeploymentDatabaseError):
        value.read()
    assert value.opened == value.closed


def test_driver_exception_cannot_expose_database_credentials(snapshot_db, monkeypatch):
    monkeypatch.setattr(
        deploy_database,
        "worker_privileges",
        lambda conn: (_ for _ in ()).throw(psycopg.OperationalError("secret password")),
    )
    with pytest.raises(DeploymentDatabaseError, match="observation failed") as error:
        snapshot_db.read()
    assert "password" not in str(error.value)
    assert snapshot_db.closed == 1
