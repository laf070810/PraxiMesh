"""Disposable PostgreSQL plus actual CPU containers; no GPU admission or receipts."""

import hashlib
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from core import gpu_resources
from core.gpu_container_recovery import BoundContainerRecovery
from core.gpu_startup import recover_controller
from core.models import GPUDevice, Submission
from judge.container_connection import ContainerConnection
from judge.container_contract import PYTHON
from judge.container_journal import ContainerJournal, PersistentRuntimeService
from judge.container_service import LocalEngine
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock
from scripts.container_journal_acceptance import save
from scripts.container_supervision_acceptance import gone, tree
from tests.test_gpu_resources import FINGERPRINT, queued
from tests.test_gpu_resources import gpu_version as gpu_version

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("RUN_CONTAINER_DB_TESTS") != "1",
        reason="Requires owned PostgreSQL and fixed CPU task image",
    ),
]


class Cutoff(Exception):
    pass


class Live:
    def __init__(self, root, user, version, case):
        assert connection.vendor == "postgresql", "Real PostgreSQL is required"
        self.root, self.case = root, case
        self.engine = LocalEngine(os.environ["PX_CONTAINER_DB_SOCKET"])
        self.image = os.environ["PX_CONTAINER_DB_IMAGE"]
        self.device = "GPU-" + str(uuid.uuid4())
        self.guard = DeviceLock(root / "locks", self.device).acquire()
        gpu_resources.adopt(self.guard, "container-db-cpu", FINGERPRINT)
        # Synthetic PRIOR reservation. Never mark a device ready or compile this source.
        self.job = queued(
            user,
            version,
            status="running",
            started_at=timezone.now(),
            lease_until=timezone.now(),
            lease_token=uuid.uuid4(),
        )
        GPUDevice.objects.filter(pk=self.device).update(
            job_id=self.job.id, reservation_token=self.job.lease_token
        )
        self.journal = ContainerJournal(root / "journal", self.guard, self.engine)
        self.service = self.connection = None
        self.processes = {}
        self.record = {
            "case": case,
            "passed": False,
            "gpu_executed": False,
            "synthetic_reservation": True,
            "cleanup": [],
        }

    def start(self, phase="running"):
        service = self.service = PersistentRuntimeService(
            self.journal, self.job.id, self.job.lease_token, image=self.image
        )
        if phase == "unrecorded-create":

            def cutoff():
                raise Cutoff

            service.record_created = cutoff
            with pytest.raises(Cutoff):
                service.start(
                    ["/bin/sleep", "30"], deadline=time.monotonic() + 50, on_tick=lambda: None
                )
            assert self.journal.load()["container_id"] is None
            self.record["interrupted_record"] = self.journal.load()
            return
        status = service.start(
            [PYTHON, "-I", "/opt/praximesh/container_transport_fixture.py", "deadline"],
            deadline=time.monotonic() + 110,
            on_tick=lambda: None,
        )
        self.connection = ContainerConnection(service, status, on_tick=lambda: None)
        canary = os.urandom(32).hex()
        self.connection.channel.send({"event": "probe", "canary": canary, "padding": "x" * 1024**2})
        value = self.connection.channel.receive()
        assert value["event"] == "guest-ready"
        assert value["identity"]["sha256"] == hashlib.sha256(canary.encode()).hexdigest()
        assert (
            value["identity"]["gpu_nodes"] == [] and not value["identity"]["private_file_visible"]
        )
        self.connection.channel.send({"event": "hang"})
        assert self.connection.channel.receive() == {"event": "busy"}
        self.processes = tree(self.engine, service)
        self.record.update(
            guest=value["identity"], processes=self.processes, original_record=self.journal.load()
        )

    def recover(self):
        value = BoundContainerRecovery(self.journal)
        value.stop()
        return value

    def finish(self, value):
        result = value.finish(job_id=self.job.id, reservation_token=self.job.lease_token)
        assert result["ready"] is False
        return result

    def database(self):
        row = GPUDevice.objects.get(pk=self.device)
        job = Submission.objects.filter(pk=self.job.id).first()
        return {
            "owner": str(row.owner_token),
            "state": row.state,
            "job": str(row.job_id),
            "reservation": str(row.reservation_token),
            "checked_at": str(row.checked_at) if row.checked_at else None,
            "submission": {"status": job.status, "budget": job.budget_state, "verdict": job.verdict}
            if job
            else None,
        }

    def retained(self):
        value = self.database()
        assert value["state"] == "quarantined" and value["checked_at"] is None
        assert value["job"] == str(self.job.id) and value["reservation"] == str(
            self.job.lease_token
        )
        assert (
            not gpu_resources.available(self.device, FINGERPRINT)
            and gpu_resources.claim(self.guard) is None
        )
        if value["submission"]:
            assert value["submission"]["status"] == "running" and not value["submission"]["verdict"]
        return value

    def passed(self, **details):
        self.record.update(passed=True, **details)


@pytest.fixture
def live(user, gpu_version, request):
    output = Path(os.environ["PX_CONTAINER_DB_REPORTS"]) / uuid.uuid4().hex
    output.mkdir(mode=0o700)
    storage = tempfile.TemporaryDirectory(prefix="px-container-db-")
    value = Live(Path(storage.name), user, gpu_version, request.node.name)
    save(output / "result.json", value.record)
    try:
        yield value
    finally:
        try:
            if value.connection is not None:
                value.connection.close()
            before = value.journal.load()
            value.record["before_test_teardown"] = before
            if before is not None:
                cleanup = value.journal.recover()
                value.record["cleanup"].append({**cleanup, "test_teardown_only": True})
            if value.service is not None:
                identifier = value.service.container_id
                assert (
                    identifier
                    not in value.engine.run(
                        "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"
                    )
                    .stdout.decode()
                    .splitlines()
                )
                value.record["container"] = identifier
                value.record["container_removed"] = True
            gone(value.processes)
            value.record["process_tree_gone"] = True
            shutil.copytree(value.root / "journal", output / "journal")
            value.journal.close()
            value.guard.close()
            storage.cleanup()
            value.record["storage_removed"] = not Path(storage.name).exists()
        except BaseException as error:
            value.record.update(
                passed=False,
                cleanup_failure=type(error).__name__ + ": " + str(error),
                retained_storage=storage.name,
            )
            storage._finalizer.detach()
            raise
        finally:
            save(output / "result.json", value.record)


@pytest.mark.parametrize("consumed", [False, True])
def test_actual_running_guest_stops_without_releasing_budget(live, consumed):
    if consumed:
        Submission.objects.filter(pk=live.job.id).update(
            budget_state="consumed", execution_started_at=timezone.now()
        )
    live.start()
    recovery = live.recover()
    assert live.journal.load() is not None
    gone(live.processes)
    result = live.finish(recovery)
    state = live.retained()
    assert state["submission"]["budget"] == ("consumed" if consumed else "reserved")
    live.passed(recovery=result, database=state)


def test_actual_create_interruption_fences_id_before_stop(live):
    live.start("unrecorded-create")
    recovery = live.recover()
    record = live.journal.load()
    assert record["container_id"] == live.service.container_id and record["phase"] == "created"
    result = live.finish(recovery)
    live.passed(fenced_record=record, recovery=result, database=live.retained())


def test_actual_account_deletion_retains_device_slot(live):
    live.start()
    live.job.user.delete()
    assert not Submission.objects.filter(pk=live.job.id).exists()
    result = live.finish(live.recover())
    live.passed(recovery=result, database=live.retained())


def test_actual_different_reservation_keeps_stopped_record(live):
    live.start()
    other = uuid.uuid4()
    GPUDevice.objects.filter(pk=live.device).update(reservation_token=other)
    recovery = live.recover()
    with pytest.raises(gpu_resources.DeviceOwnershipError, match="does not match"):
        recovery.finish(job_id=live.job.id, reservation_token=other)
    gone(live.processes)
    assert live.journal.load()["reservation"] == str(live.job.lease_token)
    assert live.service.inspect()["running"] is False
    database = live.database()
    assert database["reservation"] == str(other) and database["state"] == "quarantined"
    live.passed(refused=True, database=database, retained_record=live.journal.load())


def test_actual_postgres_adoption_timeout_stops_busy_guest(live):
    live.start()
    # Only the startup DB prefix is reachable. This object never prepares a GPU.
    adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
    adapter.target, adapter.fingerprint = {"uuid": live.device}, FINGERPRINT

    def recover():
        try:
            recover_controller(
                live.guard, "container-db-cpu", adapter, container_journal=live.journal
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            GPUDevice.objects.select_for_update().get(pk=live.device)
            started = time.monotonic()
            with pytest.raises(DatabaseError) as error:
                executor.submit(recover).result(timeout=12)
            elapsed = time.monotonic() - started
            assert error.value.__cause__.sqlstate == "55P03" and elapsed < 10
            assert live.service.inspect()["running"] is False
    gone(live.processes)
    assert live.journal.load()["container_id"] == live.service.container_id
    retained = live.journal.load()
    result = live.finish(live.recover())
    live.passed(
        sqlstate="55P03",
        stop_after_lock_failure_seconds=elapsed,
        retained_after_failure=retained,
        recovery=result,
        database=live.retained(),
    )


def test_actual_archive_interruption_and_retry(live, monkeypatch):
    live.start()
    recovery = live.recover()
    original = live.journal.write

    def fail(name, value, **kwargs):
        if name.endswith(".done.json"):
            raise OSError("fixed archive publication interruption")
        return original(name, value, **kwargs)

    monkeypatch.setattr(live.journal, "write", fail)
    with pytest.raises(OSError, match="publication interruption"):
        live.finish(recovery)
    assert live.journal.load()["phase"] == "removed"
    retained = live.journal.load()
    gone(live.processes)
    monkeypatch.setattr(live.journal, "write", original)
    result = live.finish(live.recover())
    live.passed(retained_after_failure=retained, recovery=result, database=live.retained())


def test_actual_new_lock_owner_adopts_previous_container(live):
    live.start()
    before = live.database()
    identity = live.journal.identity
    live.connection.close()
    live.connection = None
    live.journal.close()
    live.guard.close()
    live.guard = DeviceLock(live.root / "locks", live.device).acquire()
    gpu_resources.adopt(live.guard, "container-db-replacement", FINGERPRINT)
    live.journal = ContainerJournal(live.root / "journal", live.guard, live.engine)
    assert live.journal.identity == identity
    result = live.finish(live.recover())
    after = live.retained()
    assert after["owner"] != before["owner"]
    live.passed(previous_database=before, database=after, recovery=result)


def test_actual_database_change_after_archive_is_not_success(live, monkeypatch):
    live.start()
    recovery = live.recover()
    original = live.journal.recover
    replacement = uuid.uuid4()

    def changed():
        result = original()
        GPUDevice.objects.filter(pk=live.device).update(owner_token=replacement)
        return result

    monkeypatch.setattr(live.journal, "recover", changed)
    with pytest.raises(gpu_resources.DeviceOwnershipError):
        live.finish(recovery)
    assert live.journal.load() is None and list(live.journal.directory.glob("*.done.json"))
    database = live.database()
    assert database["owner"] == str(replacement) and database["state"] == "quarantined"
    assert database["job"] == str(live.job.id)
    live.passed(refused_after_archival=True, database=database)
