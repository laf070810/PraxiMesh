"""SQL ordering model only; no PostgreSQL, Docker, GPU, or real freeze proof."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import deploy_database
from scripts.deploy_database import DeploymentDatabaseError
from tests.test_deploy_database import configuration as configuration
from tests.test_deploy_database import database as database


@pytest.fixture
def freeze_model(database, monkeypatch):
    name, token = database.name("a" * 12), str(uuid.uuid4())
    owner = database.config["owner_user"]
    model = SimpleNamespace(
        name=name,
        token=token,
        held=False,
        connected=True,
        guard=False,
        pending=False,
        devices=[],
        sessions=[(11, database.roles["web"]), (12, database.roles["worker"])],
        events=[],
        prepared=False,
        hook=lambda event: None,
    )

    def event(value):
        model.events.append(value)
        model.hook(value)

    class Session:
        info = SimpleNamespace(backend_pid=77)

        def execute(self, statement, params=()):
            text = str(statement)
            result = None
            if text.startswith("LOCK TABLE"):
                model.held = True
                event("locked")
            elif text.startswith("SELECT job_id"):
                result = model.devices
            elif text.startswith("SELECT pause_token"):
                result = (model.token, "paused")
            elif "FROM public.core_submission" in text:
                result = (model.pending,)
            elif "REVOKE CONNECT" in text:
                event("revoke")
                model.connected = False
            elif "pg_terminate_backend" in text:
                pid = params[0]
                assert pid != self.info.backend_pid
                event("terminate:" + str(pid))
                model.sessions[:] = [pair for pair in model.sessions if pair[0] != pid]
            elif "has_database_privilege" in text:
                result = (model.connected,)
            elif "pg_prepared_xacts" in text:
                result = (model.prepared,)
            else:
                pytest.fail("Unexpected SQL in freeze model: " + text)
            return SimpleNamespace(fetchone=lambda: result, fetchall=lambda: result)

        @contextmanager
        def transaction(self):
            yield

    @contextmanager
    def original(target):
        assert target == name and not model.guard
        model.guard = True
        model.sessions.append((77, owner))
        try:
            yield Session()
        finally:
            model.guard = model.held = False
            model.sessions[:] = [pair for pair in model.sessions if pair[0] != 77]
            event("released")

    @contextmanager
    def admin(target, **kwargs):
        assert target == "postgres"
        yield Session()

    def owned(target):
        assert target == name

    monkeypatch.setattr(database, "_require_operation_lock", lambda: None)
    monkeypatch.setattr(database, "require_owned", owned)
    monkeypatch.setattr(database, "_require_roles", lambda: None)
    monkeypatch.setattr(database, "connection", original)
    monkeypatch.setattr(database, "_connect", admin)
    monkeypatch.setattr(database, "_sessions", lambda *a: list(model.sessions))
    monkeypatch.setattr(deploy_database, "worker_privileges", lambda connection: {"core_gpudevice"})
    return database, model


def test_gpu_freeze_holds_drain_locks_until_runtime_access_is_closed(freeze_model):
    database, model = freeze_model

    def observe(event):
        if event == "revoke" or event.startswith("terminate:"):
            assert model.held and model.guard
        if event == "released":
            assert not model.connected and model.sessions == []

    model.hook = observe
    database.freeze_gpu(model.name, model.token)
    assert model.events == ["locked", "revoke", "terminate:11", "terminate:12", "released"]
    assert not model.connected and not model.held and model.sessions == []
    # A lost successful reply is safe to retry, with a new check transaction.
    database.freeze_gpu(model.name, model.token)
    assert model.events[-3:] == ["locked", "revoke", "released"]


def test_cpu_freeze_still_refuses_a_held_owner_transaction(freeze_model):
    database, model = freeze_model
    with database.gpu_configuration_guard([model.name], model.token):
        with pytest.raises(DeploymentDatabaseError, match="Unexpected database sessions"):
            database.freeze(model.name)
    assert "revoke" not in model.events and model.connected


@pytest.mark.parametrize("defect", ["job", "reservation", "pending", "token", "owner", "prepared"])
def test_gpu_freeze_refuses_uncleared_or_foreign_state(freeze_model, defect):
    database, model = freeze_model
    token = model.token
    if defect == "job":
        model.devices = [(uuid.uuid4(), None)]
    elif defect == "reservation":
        model.devices = [(None, uuid.uuid4())]
    elif defect == "pending":
        model.pending = True
    elif defect == "token":
        model.token = str(uuid.uuid4())
    elif defect == "owner":
        model.sessions.append((78, database.config["owner_user"]))
    else:
        model.prepared = True
    before = list(model.devices)
    with pytest.raises(DeploymentDatabaseError):
        database.freeze_gpu(model.name, token)
    assert model.devices == before and not model.guard
    if defect != "prepared":
        assert "revoke" not in model.events and model.connected
    else:
        assert not model.connected  # Failed final check must not thaw automatically.


def test_unexpected_connection_after_guard_release_prevents_confirmation(freeze_model):
    database, model = freeze_model

    def appeared(event):
        if event == "released":
            model.sessions.append((78, database.config["owner_user"]))

    model.hook = appeared
    with pytest.raises(DeploymentDatabaseError, match="remain frozen"):
        database.freeze_gpu(model.name, model.token)
    assert not model.connected and model.sessions == [(78, database.config["owner_user"])]


def test_error_after_revoke_keeps_permissions_closed_and_retry_rechecks(freeze_model):
    database, model = freeze_model

    def lost(event):
        if event == "terminate:11":
            raise OSError("modeled lost control response")

    model.hook = lost
    with pytest.raises(DeploymentDatabaseError):
        database.freeze_gpu(model.name, model.token)
    assert not model.connected and not model.guard
    model.hook = lambda event: None
    database.freeze_gpu(model.name, model.token)
    assert model.sessions == [] and not model.connected
