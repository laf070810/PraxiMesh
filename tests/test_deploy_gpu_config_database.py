"""SQL refusal/order checks with explicit connection substitutes, not PG evidence."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import psycopg
import pytest

from judge.container_worker_config import database_settings
from scripts import deploy_database
from scripts.deploy_database import DeploymentDatabaseError
from tests.test_deploy_database import configuration as configuration
from tests.test_deploy_database import database as database


def test_private_worker_url_preserves_credentials_without_query(database):
    database.config["worker_password"] = "special:p@ss/word?#%"
    name = database.name("abcdef123456")
    value = database.worker_configuration(name)
    parsed = urlsplit(value["url"])
    assert not parsed.query and not parsed.fragment
    assert unquote(parsed.password) == database.config["worker_password"]
    assert parsed.username == database.roles["worker"]
    result = database_settings({"database": value})
    assert result["NAME"] == name and result["USER"] == database.roles["worker"]
    assert result["PASSWORD"] == database.config["worker_password"]
    assert database.url(name, "worker") == value["url"] + "?sslmode=disable"


@pytest.fixture
def databases(database, monkeypatch):
    token = str(uuid.uuid4())
    names = [database.name("aaaaaaaaaaaa"), database.name("bbbbbbbbbbbb")]
    model = SimpleNamespace(lock=True, acquired=[], released=[], calls=[], schemas=True)
    model.rows = {
        name: {"devices": [], "queue": (uuid.UUID(token), "paused"), "pending": False}
        for name in names
    }

    def lock():
        if not model.lock:
            raise DeploymentDatabaseError("Database deployment lock was lost")

    class Connection:
        def __init__(self, name):
            self.name = name

        def execute(self, sql):
            row = model.rows[self.name]
            model.calls.append((self.name, sql))
            if sql == "LOCK TABLE public.core_gpudevice IN SHARE ROW EXCLUSIVE MODE":
                return None
            if "FROM public.core_gpudevice" in sql:
                return SimpleNamespace(fetchall=lambda: row["devices"])
            if "FROM public.core_queuelock" in sql:
                return SimpleNamespace(fetchone=lambda: row["queue"])
            if "FROM public.core_submission" in sql:
                return SimpleNamespace(fetchone=lambda: (row["pending"],))
            pytest.fail("Unexpected SQL " + sql)

    @contextmanager
    def connection(name):
        database._validate_name(name)
        model.acquired.append(name)
        try:
            yield Connection(name)
        finally:
            model.released.append(name)

    monkeypatch.setattr(database, "_require_operation_lock", lock)
    monkeypatch.setattr(database, "connection", connection)
    monkeypatch.setattr(
        deploy_database,
        "worker_privileges",
        lambda conn: {"core_gpudevice": "recorded"} if model.schemas else {},
    )
    return database, model, names, token


def test_guard_holds_both_original_databases_in_stable_order_and_rechecks(databases):
    database, model, names, token = databases
    with database.gpu_configuration_guard(list(reversed(names)), token) as check:
        assert model.acquired == names and not model.released
        check()
        assert not model.released
    assert model.released == list(reversed(names))
    assert model.calls[:2] == [
        (name, "LOCK TABLE public.core_gpudevice IN SHARE ROW EXCLUSIVE MODE") for name in names
    ]
    assert len(model.calls) == 20
    for offset in range(2, len(model.calls), 3):
        chunk = model.calls[offset : offset + 3]
        assert "core_gpudevice" in chunk[0][1] and "FOR UPDATE" in chunk[0][1]
        assert "core_queuelock" in chunk[1][1] and "FOR UPDATE" in chunk[1][1]
        assert "('queued', 'running')" in chunk[2][1]


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize(
    "defect",
    ["token", "missing-queue", "unpaused", "pending", "orphan-reservation", "orphan-job", "both"],
)
def test_either_database_pause_or_physical_reservation_blocks_publication(databases, side, defect):
    database, model, names, token = databases
    row = model.rows[names[side]]
    if defect == "token":
        row["queue"] = (uuid.uuid4(), "paused")
    elif defect == "missing-queue":
        row["queue"] = None
    elif defect == "unpaused":
        row["queue"] = (uuid.UUID(token), None)
    elif defect == "pending":
        row["pending"] = True
    elif defect == "orphan-reservation":
        row["devices"] = [(None, uuid.uuid4())]
    elif defect == "orphan-job":
        row["devices"] = [(uuid.uuid4(), None)]
    else:
        row["devices"] = [(uuid.uuid4(), uuid.uuid4())]
    with pytest.raises(DeploymentDatabaseError, match="fully drained"):
        with database.gpu_configuration_guard(names, token):
            pytest.fail("Unsafe database was accepted")
    assert set(model.released) == set(names)


@pytest.mark.parametrize(
    "token", [None, 1, "bad", "A" * 32, "a" * 32, "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}"]
)
def test_invalid_operation_tokens_fail_before_connecting(databases, token):
    database, model, names, _ = databases
    with pytest.raises(DeploymentDatabaseError, match="original databases"):
        with database.gpu_configuration_guard(names, token):
            pytest.fail("Invalid token accepted")
    assert not model.acquired


def test_guard_needs_recorded_schema_and_live_operation_lock(databases):
    database, model, names, token = databases
    model.schemas = False
    with pytest.raises(DeploymentDatabaseError, match="recorded GPU schema"):
        with database.gpu_configuration_guard(names, token):
            pytest.fail("Missing schema accepted")
    model.schemas = True
    with pytest.raises(DeploymentDatabaseError, match="lock was lost"):
        with database.gpu_configuration_guard(names, token) as check:
            model.lock = False
            check()


def test_database_error_cannot_expose_connection_credentials(databases, monkeypatch):
    database, _, names, token = databases
    monkeypatch.setattr(
        deploy_database,
        "worker_privileges",
        lambda conn: (_ for _ in ()).throw(
            psycopg.OperationalError("private password in driver error")
        ),
    )
    with pytest.raises(DeploymentDatabaseError, match="database checks failed") as error:
        with database.gpu_configuration_guard(names, token):
            pytest.fail("Connection error accepted")
    assert "password" not in str(error.value)
