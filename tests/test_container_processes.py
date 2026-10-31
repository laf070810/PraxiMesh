"""Ownership refusal boundaries; real namespace evidence uses container acceptance."""

import copy
import os
from types import SimpleNamespace

import pytest

from judge import container_processes as module
from judge.container_processes import (
    HostProcessView,
    OwnedProcesses,
    process_identity,
    readonly_mounts,
)
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import intent, journal  # noqa: F401
from tests.test_container_supervision import ID


def stat_line(pid=1234, birth=100, state="S"):
    return f"{pid} (fixed ) process name) " + " ".join(
        [state, *("0" for _ in range(18)), str(birth), "0"]
    )


def test_proc_identity_preserves_cgroup_namespace_relative_path():
    value = process_identity(1234, stat_line(), "NSpid:\t1234\t1\n", "0::/../docker-abc.scope\n")
    assert value == {
        "pid": 1234,
        "birth": 100,
        "nspids": [1234, 1],
        "cgroup": "0::/../docker-abc.scope",
    }


@pytest.mark.parametrize(
    "change",
    [
        "pid",
        "birth",
        "zombie",
        "duplicate-nspid",
        "wrong-view",
        "empty-nspid",
        "multiple-groups",
        "v1-group",
    ],
)
def test_incomplete_or_changed_process_metadata_is_not_ownership(change):
    process_stat, status, group = stat_line(), "NSpid:\t1234\t1\n", "0::/../docker-abc.scope\n"
    if change == "pid":
        process_stat = stat_line(1235)
    elif change == "birth":
        process_stat = stat_line(birth=0)
    elif change == "zombie":
        process_stat = stat_line(state="Z")
    elif change == "duplicate-nspid":
        status *= 2
    elif change == "wrong-view":
        status = "NSpid:\t1\n"
    elif change == "empty-nspid":
        status = "NSpid:\n"
    elif change == "multiple-groups":
        group += "0::/other\n"
    else:
        group = "1:cpu:/group\n"
    with pytest.raises(SandboxUnavailable):
        process_identity(1234, process_stat, status, group)


def mount_line(path=None, options="ro,nosuid,nodev,noexec", propagation="", kind="proc"):
    return f"30 20 0:22 / {path or module.HOST_PROC} {options} {propagation} - {kind} proc rw\n"


@pytest.mark.parametrize(
    "text",
    [
        "",
        mount_line(options="rw"),
        mount_line(kind="tmpfs"),
        mount_line(propagation="shared:1"),
        mount_line() * 2,
        mount_line() + mount_line(str(module.HOST_PROC) + "/sys", options="rw"),
        mount_line() + mount_line(str(module.HOST_PROC) + "/sys", propagation="master:2"),
    ],
)
def test_view_refuses_wrong_filesystem_writable_children_or_shared_propagation(text):
    with pytest.raises(SandboxUnavailable):
        readonly_mounts(text)


def test_all_proc_submounts_must_be_readonly_and_private():
    readonly_mounts(mount_line() + mount_line(str(module.HOST_PROC) + "/sys"))


@pytest.fixture
def view(tmp_path):
    process = tmp_path / "1234"
    process.mkdir()
    (process / "stat").write_text(stat_line())
    (process / "status").write_text("NSpid:\t1234\t1\n")
    (process / "cgroup").write_text("0::/../docker-test.scope\n")
    instance = HostProcessView.__new__(HostProcessView)
    instance.fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    instance.check = lambda: None
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("pid", [True, 0, -1, 2**31, "1234", "../1", None])
def test_observer_never_uses_arbitrary_paths(view, pid):
    with pytest.raises(SandboxUnavailable):
        view.read(pid)


def test_one_observation_refuses_pid_reuse_or_migration(view, monkeypatch):
    original = module.read_file
    calls = 0

    def replaced(directory, name, maximum):
        nonlocal calls
        if name == "stat":
            calls += 1
            return stat_line(birth=100 if calls == 1 else 101)
        return original(directory, name, maximum)

    monkeypatch.setattr(module, "read_file", replaced)
    with pytest.raises(SandboxUnavailable, match="changed during observation"):
        view.read(1234)


def test_missing_process_and_symlink_are_not_treated_as_empty(view, tmp_path):
    with pytest.raises(SandboxUnavailable):
        view.read(5678)
    (tmp_path / "5678").symlink_to(tmp_path / "1234")
    with pytest.raises(SandboxUnavailable):
        view.read(5678)


@pytest.fixture
def observed_task(journal):  # noqa: F811
    service, state = intent(journal)
    service.inspect()
    journal.update(service, "created")
    journal.update(service, "running")
    group = "0::/../docker-" + ID + ".scope"
    members = {
        1234: {"pid": 1234, "birth": 100, "nspids": [1234, 1], "cgroup": group},
        5678: {"pid": 5678, "birth": 105, "nspids": [5678, 9], "cgroup": group},
        9999: {
            "pid": 9999,
            "birth": 99,
            "nspids": [9999],
            "cgroup": "0::/../../../session-1.scope",
        },
    }

    def read(pid):
        if pid not in members:
            raise SandboxUnavailable("Process disappeared")
        return copy.deepcopy(members[pid])

    control = SimpleNamespace(
        journal=journal, check=journal.check, host_processes=SimpleNamespace(read=read)
    )
    observer = OwnedProcesses(control, service)
    return SimpleNamespace(
        observer=observer,
        control=control,
        service=service,
        state=state,
        members=members,
        journal=journal,
    )


def test_task_membership_uses_original_cgroup_and_birth(observed_task):
    task = observed_task
    assert task.observer.require_owned([1234, 5678]) == [task.members[1234], task.members[5678]]
    assert task.observer.require_owned([]) == []
    with pytest.raises(SandboxUnavailable, match="outside the original task"):
        task.observer.require_owned([1234, 9999])
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")}
        for call in task.journal.engine.calls
    )


@pytest.mark.parametrize(
    "inventory", [[True], [0], [-1], [2**31], [1234, 1234], ["1234"], tuple([1234]), [1234] * 4097]
)
def test_malformed_inventory_is_not_empty_or_owned(observed_task, inventory):
    with pytest.raises(SandboxUnavailable, match="Invalid host process inventory"):
        observed_task.observer.require_owned(inventory)


@pytest.mark.parametrize(
    "change", ["pid1-reuse", "pid1-migration", "stopped", "restarted", "lost-record", "view"]
)
def test_live_fence_refuses_stale_or_replaced_ownership(observed_task, change):
    task = observed_task
    if change == "pid1-reuse":
        task.members[1234]["birth"] += 1
    elif change == "pid1-migration":
        task.members[1234]["cgroup"] = "0::/different"
    elif change == "stopped":
        task.state["State"].update(Running=False, Status="exited", Pid=0)
    elif change == "restarted":
        task.state["State"]["StartedAt"] = "2026-11-01T00:00:00Z"
    elif change == "lost-record":
        task.journal.recover()
    else:
        task.members[1234]["nspids"] = [1234]
    with pytest.raises(SandboxUnavailable):
        task.observer.require_owned([])


def test_inventory_pid_reuse_between_complete_reads_is_rejected(observed_task):
    task = observed_task
    original = task.control.host_processes.read
    calls = 0

    def read(pid):
        nonlocal calls
        value = original(pid)
        if pid == 5678:
            calls += 1
            value["birth"] += calls - 1
        return value

    task.control.host_processes.read = read
    with pytest.raises(SandboxUnavailable, match="Observed process identity changed"):
        task.observer.require_owned([5678])
