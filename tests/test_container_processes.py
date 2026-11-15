"""Ownership refusal boundaries; real namespace evidence uses container acceptance."""

import copy
import errno
import os
from types import SimpleNamespace

import pytest

from judge import container_processes as module
from judge.container_processes import (
    HostProcessView,
    OwnedProcesses,
    ProcessDisappeared,
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


@pytest.mark.parametrize(
    "options,accepted",
    [
        (["name=rootless", "name=seccomp,profile=builtin", "name=cgroupns"], True),
        (["name=rootless", "name=seccomp,profile=builtin", "name=selinux"], False),
        (["name=rootless", "name=seccomp,profile=builtin", "name=selinux,enabled"], False),
        (["name=rootless"], False),
        (["name=seccomp,profile=builtin"], False),
        (None, False),
    ],
)
def test_host_pid_profile_does_not_disable_active_selinux_or_seccomp(options, accepted):
    if accepted:
        assert module.host_pid_security_options({"SecurityOptions": options}) == [
            "no-new-privileges",
            "label=disable",
        ]
    else:
        with pytest.raises(SandboxUnavailable):
            module.host_pid_security_options({"SecurityOptions": options})


def test_proc_identity_preserves_cgroup_namespace_relative_path():
    value = process_identity(1234, stat_line(), "NSpid:\t1234\t1\n", "0::/../docker-abc.scope\n")
    assert value == {
        "pid": 1234,
        "birth": 100,
        "nspids": [1234, 1],
        "cgroup": "0::/../docker-abc.scope",
    }


def test_systemd_user_unit_cgroup_is_kept_as_opaque_identity():
    group = "0::/user.slice/user-2031.slice/user@2031.service/app.slice/owned.scope\n"
    value = process_identity(1234, stat_line(), "NSpid:\t1234\n", group)
    assert value["cgroup"] == group.strip()


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


@pytest.mark.parametrize(
    "defect",
    [None, "private-pid", "nested", "missing", "duplicate", "unreadable", "birth-change"],
)
def test_management_observer_requires_the_callers_complete_pid_domain(
    view, tmp_path, monkeypatch, defect
):
    (tmp_path / "self").symlink_to("1234")
    (tmp_path / "1234/status").write_text("NSpid:\t1234\n")
    monkeypatch.setattr(module.os, "getpid", lambda: 1234)
    if defect == "private-pid":
        monkeypatch.setattr(module.os, "getpid", lambda: 1)
        # Host init could look valid if the check used the caller's numeric PID.
        (tmp_path / "1").mkdir()
        (tmp_path / "1/stat").write_text(stat_line(pid=1))
        (tmp_path / "1/status").write_text("NSpid:\t1\n")
        (tmp_path / "1/cgroup").write_text("0::/\n")
        (tmp_path / "1234/status").write_text("NSpid:\t1234\t1\n")
    elif defect == "nested":
        (tmp_path / "1234/status").write_text("NSpid:\t1234\t1234\n")
    elif defect == "missing":
        (tmp_path / "1234/status").write_text("Pid:\t1234\n")
    elif defect == "duplicate":
        (tmp_path / "1234/status").write_text("NSpid:\t1234\nNSpid:\t1234\n")
    elif defect in {"unreadable", "birth-change"}:
        original = module.read_file
        reads = 0

        def changed(directory, name, maximum):
            nonlocal reads
            if name == "stat":
                reads += 1
                if defect == "unreadable":
                    raise PermissionError(errno.EACCES, "Fixed denial")
                return stat_line(birth=100 if reads == 1 else 101)
            return original(directory, name, maximum)

        monkeypatch.setattr(module, "read_file", changed)
    if defect is None:
        view.require_same_pid_namespace()
    else:
        with pytest.raises(SandboxUnavailable):
            view.require_same_pid_namespace()


def test_missing_process_is_distinct_from_a_permission_failure(view, monkeypatch):
    with pytest.raises(ProcessDisappeared) as missing:
        view.read(5678)
    assert missing.value.identity is None
    assert module.process_failure_details(missing.value) == {"pid": 5678, "field": "directory"}
    original = module.read_file

    def denied(directory, name, maximum):
        if name == "stat":
            raise PermissionError(errno.EACCES, "Fixed permission failure")
        return original(directory, name, maximum)

    monkeypatch.setattr(module, "read_file", denied)
    with pytest.raises(SandboxUnavailable) as error:
        view.read(1234)
    assert type(error.value) is SandboxUnavailable


@pytest.mark.parametrize(
    "pid,field",
    [
        (True, "stat"),
        (0, "stat"),
        (2**31, "stat"),
        (1234, "secret"),
        (1234, []),
        ("1234", "status"),
    ],
)
def test_process_diagnostics_are_bounded_and_never_supply_ownership(pid, field):
    error = ProcessDisappeared("private message", requested_pid=pid, field=field)
    assert module.process_failure_details(error) == {} and error.identity is None


def test_missing_proc_field_is_reported_even_when_kernel_error_has_no_filename(view, monkeypatch):
    original = module.read_file

    def failed(directory, name, maximum):
        if name == "status":
            raise FileNotFoundError(errno.ENOENT, "private error text")
        return original(directory, name, maximum)

    monkeypatch.setattr(module, "read_file", failed)
    with pytest.raises(ProcessDisappeared) as caught:
        view.read(1234)
    assert caught.value.identity is None
    assert module.process_failure_details(caught.value) == {"pid": 1234, "field": "status"}


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_readable_exited_identity_is_not_a_liveness_or_missing_pid_exemption(view, tmp_path, state):
    (tmp_path / "1234/stat").write_text(stat_line(state=state))
    with pytest.raises(ProcessDisappeared):
        view.read(1234)
    found = view.read_observed(1234)
    assert found["pid"] == 1234 and found["birth"] == 100
    (tmp_path / "1234/stat").unlink()
    with pytest.raises(ProcessDisappeared) as error:
        view.read_observed(1234)
    assert error.value.identity is None


@pytest.mark.parametrize("change", ["reaped", "reused", "moved"])
def test_exited_member_still_needs_two_complete_matching_reads(view, tmp_path, monkeypatch, change):
    (tmp_path / "1234/stat").write_text(stat_line(state="Z"))
    original = module.read_file
    counts = {}

    def read(directory, name, maximum):
        counts[name] = counts.get(name, 0) + 1
        if name == "stat" and counts[name] == 2:
            if change == "reaped":
                raise FileNotFoundError(errno.ENOENT, "reaped")
            if change == "reused":
                return stat_line(birth=101, state="Z")
        if name == "cgroup" and counts[name] == 2 and change == "moved":
            return "0::/elsewhere\n"
        return original(directory, name, maximum)

    monkeypatch.setattr(module, "read_file", read)
    with pytest.raises(SandboxUnavailable):
        view.read_observed(1234)


def test_exited_process_retains_birth_and_cgroup_for_original_identity_check():
    with pytest.raises(ProcessDisappeared) as error:
        process_identity(1234, stat_line(state="Z"), "NSpid:\t1234\t1\n", "0::/original\n")
    assert error.value.identity == {
        "pid": 1234,
        "birth": 100,
        "nspids": [1234, 1],
        "cgroup": "0::/original",
    }


@pytest.fixture
def observed_task(journal, request):  # noqa: F811
    service, state = intent(journal, purpose=getattr(request, "param", "task"))
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
            raise ProcessDisappeared("Process disappeared")
        return copy.deepcopy(members[pid])

    control = SimpleNamespace(
        journal=journal, check=journal.check, host_processes=SimpleNamespace(read=read)
    )
    control.host_processes.read_observed = lambda pid: control.host_processes.read(pid)
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


def test_observed_exited_child_is_owned_but_original_pid_one_must_remain_live(
    observed_task, view, tmp_path
):
    task = observed_task
    group = task.members[1234]["cgroup"]
    (tmp_path / "1234/cgroup").write_text(group)
    child = tmp_path / "5678"
    child.mkdir()
    (child / "stat").write_text(stat_line(pid=5678, birth=105, state="Z"))
    (child / "status").write_text("NSpid:\t5678\t9\n")
    (child / "cgroup").write_text(group)
    task.control.host_processes = view
    assert task.observer.observe_owned(lambda: [5678]) == [5678]
    with pytest.raises(ProcessDisappeared):
        task.observer.require_owned([5678])
    (child / "cgroup").write_text("0::/another-task\n")
    with pytest.raises(SandboxUnavailable, match="outside the original task"):
        task.observer.observe_owned(lambda: [5678])
    (child / "cgroup").write_text(group)
    (tmp_path / "1234/stat").write_text(stat_line(state="Z"))
    with pytest.raises(ProcessDisappeared):
        task.observer.observe_owned(lambda: [])


def test_child_oom_marker_during_observation_does_not_replace_live_task(observed_task):
    task = observed_task
    original = task.service.inspect
    first = True

    def inspect():
        nonlocal first
        value = original()
        if first:
            first = False
            task.state["State"]["OOMKilled"] = True
        return value

    task.service.inspect = inspect
    assert task.observer.require_owned([5678]) == [task.members[5678]]
    assert task.state["State"]["Running"] is True
    assert task.state["State"]["Pid"] == 1234
    assert task.state["State"]["OOMKilled"] is True


@pytest.mark.parametrize("change", ["marker-reset", "stopped", "exit-code", "birth", "group"])
def test_oom_marker_never_hides_another_state_or_process_change(observed_task, change):
    task = observed_task
    if change == "marker-reset":
        task.state["State"]["OOMKilled"] = True
    original = task.service.inspect
    first = True

    def inspect():
        nonlocal first
        value = original()
        if first:
            first = False
            task.state["State"]["OOMKilled"] = change != "marker-reset"
            if change == "stopped":
                task.state["State"].update(Running=False, Status="exited", Pid=0)
            elif change == "exit-code":
                task.state["State"]["ExitCode"] = 137
            elif change == "birth":
                task.members[1234]["birth"] += 1
            elif change == "group":
                task.members[1234]["cgroup"] = "0::/different"
        return value

    task.service.inspect = inspect
    with pytest.raises(SandboxUnavailable):
        task.observer.require_owned([])


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
