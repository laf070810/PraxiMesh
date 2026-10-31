"""Durable ownership and refusal checks; real container crashes use acceptance."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from judge import container_journal
from judge.container_journal import ContainerJournal, PersistentRuntimeService, RecoveryContainer
from judge.container_service import PREFIX, digest
from judge.gpu_device_lock import DeviceLock
from judge.sandbox import SandboxUnavailable
from tests.test_container_supervision import ID, IMAGE, snapshot


class Engine:
    def __init__(self):
        self.identifier = str(uuid.uuid4())
        self.state = None
        self.calls = []

    def verify(self):
        pass

    def run(self, *args, **_kwargs):
        self.calls.append(args)
        code, data = 0, b""
        if args[:1] == ("info",):
            data = self.identifier.encode()
        elif args[:2] == ("container", "inspect"):
            if self.state is None:
                code = 1
            else:
                data = json.dumps([self.state]).encode()
        elif args[:2] == ("container", "ls"):
            data = b"" if self.state is None else self.state["Id"].encode()
        elif args[:2] == ("container", "kill"):
            self.state["State"].update(Running=False, Status="exited", Pid=0)
        elif args[:2] == ("container", "rm"):
            assert not self.state["State"]["Running"]
            self.state = None
        else:
            pytest.fail("Unexpected operation: " + repr(args))
        return SimpleNamespace(returncode=code, stdout=data)


@pytest.fixture
def journal(tmp_path):
    with DeviceLock(tmp_path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        with ContainerJournal(tmp_path / "journal", guard, Engine()) as value:
            yield value


def intent(journal):
    service = PersistentRuntimeService(journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE)
    service.image_env = []
    state = snapshot(service)
    state["Config"]["Labels"].update(service.required_image_labels())
    service.record_intent(state["Config"]["Cmd"])
    state["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(journal.load()["security"][1]),
    ]
    journal.engine.state = state
    return service, state


def test_installation_is_private_stable_and_exclusive(journal):
    path = journal.directory / "installation.json"
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        journal.write("installation.json", {"replacement": True}, create=True)
    assert path.read_bytes() == original
    assert not list(journal.directory.glob(".write-*"))
    with ContainerJournal(journal.directory, journal.guard, journal.engine) as other:
        assert other.identity == journal.identity


def test_other_lock_namespace_and_changed_engine_are_rejected(journal, tmp_path):
    with DeviceLock(tmp_path / "other-locks", journal.guard.device_uuid) as other:
        with pytest.raises(SandboxUnavailable, match="lock namespace"):
            ContainerJournal(journal.directory, other, journal.engine)
    journal.engine.identifier = str(uuid.uuid4())
    with pytest.raises(SandboxUnavailable, match="daemon identity"):
        journal.load()
    assert not any(call[0] == "container" for call in journal.engine.calls)


def test_changed_installation_identity_is_detected_by_open_journal(journal):
    journal.write("installation.json", {**journal.identity, "installation": uuid.uuid4().hex})
    with pytest.raises(SandboxUnavailable, match="installation identity changed"):
        journal.load()


def test_publication_lock_replacement_keeps_previous_state(journal):
    previous = journal.read("installation.json")
    path = journal.directory / "publication.lock"
    path.rename(journal.directory / "old.lock")
    path.touch(mode=0o600)
    with pytest.raises(SandboxUnavailable, match="publication lock was replaced"):
        journal.write("installation.json", {"changed": True})
    assert journal.read("installation.json") == previous


def test_competing_publications_never_overwrite_an_existing_record(journal):
    def attempt(number):
        try:
            journal.write("fixed.json", {"writer": number}, create=True)
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert sum(outcomes) == 1
    assert outcomes[journal.read("fixed.json")["writer"]]
    assert not list(journal.directory.glob(".write-*"))


def test_atomic_replace_failure_leaves_the_previous_complete_record(journal, monkeypatch):
    previous = journal.read("installation.json")

    def fail(*_args, **_kwargs):
        raise OSError("fixed storage failure")

    monkeypatch.setattr(container_journal.os, "replace", fail)
    with pytest.raises(OSError, match="storage failure"):
        journal.write("installation.json", {"incomplete": True})
    assert journal.read("installation.json") == previous
    assert not list(journal.directory.glob(".write-*"))


def test_owner_uses_persistent_scope_instead_of_current_uid(journal, monkeypatch):
    first = PersistentRuntimeService(journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE)
    monkeypatch.setattr(container_journal.os, "getuid", lambda: 0)
    # Identity was already verified using real private filesystem ownership.
    monkeypatch.setattr(journal, "check_engine", lambda: None)
    same = PersistentRuntimeService(journal, first.job_id, first.reservation_token, image=IMAGE)
    assert same.owner == first.owner and same.name == first.name


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "permissions", "malformed", "oversized"]
)
def test_untrusted_record_shapes_do_not_authorize_engine_mutation(journal, kind):
    intent(journal)
    path = journal.directory / journal.filename
    original = path.read_bytes()
    path.unlink()
    if kind == "symlink":
        path.symlink_to("installation.json")
    elif kind == "hardlink":
        os.link(journal.directory / "installation.json", path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(
            b"{"
            if kind == "malformed"
            else b" " * (container_journal.MAX_RECORD + 1)
            if kind == "oversized"
            else original
        )
        path.chmod(0o644 if kind == "permissions" else 0o600)
    with pytest.raises((SandboxUnavailable, OSError)):
        journal.recover()
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")} for call in journal.engine.calls
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("device", "GPU-wrong"),
        ("owner", "0" * 64),
        ("phase", "ready"),
        ("container_id", "short"),
        ("purpose", "unknown"),
    ],
)
def test_invalid_schema_or_identity_is_retained(journal, field, value):
    intent(journal)
    record = journal.load()
    record[field] = value
    journal.write(journal.filename, record)
    with pytest.raises(SandboxUnavailable, match="durable container"):
        journal.recover()
    assert journal.read(journal.filename) == record


def test_directory_replacement_and_path_traversal_are_refused(journal):
    with pytest.raises(ValueError, match="basename"):
        journal.read("../installation.json")
    original = journal.directory
    original.rename(original.with_name("saved"))
    original.mkdir(mode=0o700)
    with pytest.raises(SandboxUnavailable, match="replaced"):
        journal.load()


def test_unrecovered_intent_prevents_another_launch(journal):
    service, state = intent(journal)
    with pytest.raises(SandboxUnavailable, match="previous device"):
        journal.begin(service, state["Config"]["Cmd"])


def test_recovery_binds_lost_create_before_kill_and_archives_after_removal(journal):
    _service, state = intent(journal)
    original_run = journal.engine.run

    def run(*args, **kwargs):
        if args[:2] == ("container", "kill"):
            assert journal.read(journal.filename)["container_id"] == ID
        return original_run(*args, **kwargs)

    journal.engine.run = run
    result = journal.recover()
    assert result["removed"] is True and result["ready"] is False
    assert journal.load() is None
    archive = journal.read(result["history"])
    assert archive["container_id"] == ID and archive["phase"] == "removed"
    assert archive["arguments"] == state["Config"]["Cmd"]
    assert not any(
        call[0] == "image" or call[:2] in {("container", "start"), ("container", "exec")}
        for call in journal.engine.calls
    )


@pytest.mark.parametrize("change", ["id", "invocation", "arguments", "security", "privileged"])
def test_replacement_or_config_drift_never_stops_a_container(journal, change):
    service, state = intent(journal)
    service.container_id = ID
    service.record_created()
    service.started_at = state["State"]["StartedAt"]
    service.record_started({})
    if change == "id":
        state["Id"] = "e" * 64
    elif change == "invocation":
        state["State"]["StartedAt"] = "2026-11-01T00:00:00Z"
    elif change == "arguments":
        state["Config"]["Cmd"][2] = "d" * 32
        state["Config"]["Labels"][PREFIX + "command"] = digest(state["Config"]["Cmd"])
    elif change == "security":
        state["HostConfig"]["SecurityOpt"] = ["no-new-privileges", "seccomp=unconfined"]
    else:
        state["HostConfig"]["Privileged"] = True
    with pytest.raises(SandboxUnavailable):
        journal.recover()
    assert journal.load()["phase"] == "running"
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")} for call in journal.engine.calls
    )


def test_old_contract_can_only_be_stopped_not_reexecuted(journal, monkeypatch):
    _service, state = intent(journal)
    record = journal.load()
    record["labels"][PREFIX + "runtime"] = "0" * 64
    state["Config"]["Labels"][PREFIX + "runtime"] = "0" * 64
    journal.write(journal.filename, record)
    monkeypatch.setattr("judge.container_runtime_service.contract_digest", lambda: "f" * 64)
    recovery = RecoveryContainer(journal, record)
    with pytest.raises(SandboxUnavailable, match="cannot execute"):
        recovery.start(["/bin/false"])
    assert journal.recover()["removed"] is True


def test_archive_failure_retains_active_recovery_record(journal, monkeypatch):
    intent(journal)
    write = journal.write

    def interrupted(name, value, **kwargs):
        if name.endswith(".done.json"):
            raise OSError("fixed archive interruption")
        return write(name, value, **kwargs)

    monkeypatch.setattr(journal, "write", interrupted)
    with pytest.raises(OSError, match="interruption"):
        journal.recover()
    assert journal.load()["phase"] == "removed"
    monkeypatch.setattr(journal, "write", write)
    assert journal.recover()["removed"] is True
    assert journal.load() is None


def test_conflicting_archive_does_not_erase_pending_record(journal):
    intent(journal)
    record = journal.load()
    archive = journal.guard.device_uuid + "." + record["arguments"][2] + ".done.json"
    journal.write(archive, {"conflict": True}, create=True)
    with pytest.raises(SandboxUnavailable, match="archive identity"):
        journal.recover()
    assert journal.load()["phase"] == "removed"
    assert journal.read(archive) == {"conflict": True}
