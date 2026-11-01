"""Read-only SQL and health response models; no database server or HTTP request."""

import io
import json
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import deploy
from scripts.deploy_database import DeploymentDatabaseError
from tests.test_deploy import private_config as private_config
from tests.test_deploy_database import configuration as configuration
from tests.test_deploy_database import database as database
from tests.test_deploy_gpu_readiness_database import snapshot_db as snapshot_db


@pytest.mark.parametrize("opened", [False, True])
def test_exposure_snapshot_observes_original_pause_or_open_without_writes(
    snapshot_db, database, opened
):
    value = snapshot_db
    row = list(value.row)
    if opened:
        row[1] = row[2] = None
    row[12] = row[13] = True
    value.row = tuple(row)
    result = database.gpu_exposure_snapshot(value.name, value.token, value.device)
    assert result["admission"] == ("open" if opened else "paused")
    assert result["pending"] and result["reservations"]
    assert value.row == tuple(row)
    assert value.opened == value.closed == 1
    assert len(value.calls) == 2 and value.calls[0][0] == "SET TRANSACTION READ ONLY"
    assert "FOR UPDATE" not in value.calls[1][0] and "LOCK TABLE" not in value.calls[1][0]
    if opened:
        with pytest.raises(DeploymentDatabaseError, match="original admission pause"):
            value.read()


@pytest.mark.parametrize(
    "defect", ["foreign", "orphan-token", "orphan-time", "missing", "schema", "lock"]
)
def test_exposure_snapshot_refuses_foreign_or_inconsistent_admission(snapshot_db, database, defect):
    value = snapshot_db
    row = list(value.row)
    if defect == "foreign":
        row[1] = uuid.uuid4()
    elif defect == "orphan-token":
        row[2] = None
    elif defect == "orphan-time":
        row[1] = None
    elif defect == "schema":
        value.schema = False
    elif defect == "lock":
        value.lock = False
    value.row = None if defect == "missing" else tuple(row)
    with pytest.raises(DeploymentDatabaseError):
        database.gpu_exposure_snapshot(value.name, value.token, value.device)
    assert value.opened == value.closed


@pytest.mark.parametrize("gpu", ["available", "unavailable", None, True])
def test_external_gpu_health_requires_explicit_application_confirmation(
    private_config, monkeypatch, gpu
):
    _, config = private_config
    host = SimpleNamespace(config=config)
    payload = {"status": "ok", "evaluation": "available"}
    if gpu is not None:
        payload["gpu_evaluation"] = gpu

    def opened(request, timeout):
        assert request.full_url == config["proxy"]["origin"] + "/health/" and timeout == 10
        return nullcontext(io.BytesIO(json.dumps(payload).encode()))

    monkeypatch.setattr(
        deploy.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=opened)
    )
    # Old CPU-only releases keep their existing health contract.
    deploy.Deployment.health(host, {}, external=True)
    if gpu == "available":
        deploy.Deployment.health(host, {}, external=True, require_gpu=True)
    else:
        with pytest.raises(deploy.DeploymentError, match="health check failed"):
            deploy.Deployment.health(host, {}, external=True, require_gpu=True)
