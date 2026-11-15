"""Maintenance-only exit races with original state; no hardware evidence."""

import copy

import pytest

from judge.container_processes import ProcessDisappeared
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import journal as journal
from tests.test_container_monitor import bundle as bundle
from tests.test_container_monitor import monitored as monitored
from tests.test_container_monitor import saved as saved
from tests.test_container_processes import observed_task as observed_task
from tests.test_smi_inventory import combined as combined


def stopped(item):
    item.task.state["State"].update(Running=False, Status="exited", Pid=0, ExitCode=70)


@pytest.mark.parametrize("observed_task", ["maintenance"], indirect=True)
class TestTermination:
    def test_query_exit_preserves_anchor_and_requeries_after_original_stop(
        self, combined, monkeypatch
    ):
        item = combined
        item.monitor.observe(item.task.service)
        original = item.monitor.owned
        calls = []

        def read():
            calls.append("read")
            stopped(item)
            return copy.deepcopy(item.value)

        monkeypatch.setattr(item.monitor, "read", read)
        assert item.monitor.observe_termination(item.task.service) == []
        assert len(calls) == 2 and item.monitor.owned is original
        assert item.task.journal.load() == original.record
        with pytest.raises(SandboxUnavailable, match="no longer running"):
            item.monitor.observe(item.task.service)

    @pytest.mark.parametrize("moment", ["pid-read", "final-inspect"])
    def test_exit_during_empty_process_fence_is_confirmed(self, combined, monkeypatch, moment):
        item = combined
        item.monitor.observe(item.task.service)
        if moment == "pid-read":

            def read(_pid):
                stopped(item)
                raise ProcessDisappeared("Original process disappeared")

            monkeypatch.setattr(item.task.control.host_processes, "read", read)
        else:
            original = item.task.control.host_processes.read

            def read(pid):
                value = original(pid)
                stopped(item)
                return value

            monkeypatch.setattr(item.task.control.host_processes, "read", read)
        assert item.monitor.observe_termination(item.task.service) == []

    @pytest.mark.parametrize(
        "defect",
        [
            "foreign",
            "stopped-nonempty",
            "query",
            "record",
            "restart",
            "birth",
            "group",
            "permission",
            "unconfirmed-disappearance",
            "reused-zombie",
        ],
    )
    def test_exit_mode_never_hides_foreign_or_unconfirmed_observations(
        self, combined, monkeypatch, defect
    ):
        item = combined
        item.monitor.observe(item.task.service)
        if defect in {"foreign", "stopped-nonempty"}:
            item.value["pids"]["inventory"] = [9999 if defect == "foreign" else 5678]
            if defect == "stopped-nonempty":
                stopped(item)
        elif defect == "query":

            def read():
                stopped(item)
                raise SandboxUnavailable("Complete query failed")

            monkeypatch.setattr(item.monitor, "read", read)
        elif defect == "record":
            item.task.journal.recover()
        elif defect == "restart":
            stopped(item)
            item.task.state["State"]["StartedAt"] = "2026-11-01T00:00:00Z"
        elif defect == "birth":
            item.task.members[1234]["birth"] += 1
        elif defect == "group":
            item.task.members[1234]["cgroup"] = "0::/different"
        else:

            def read(_pid):
                if defect == "permission":
                    stopped(item)
                    raise SandboxUnavailable("Permission denied")
                if defect == "reused-zombie":
                    stopped(item)
                    identity = dict(item.task.members[1234], birth=999)
                    raise ProcessDisappeared("Exited replacement", identity=identity)
                raise ProcessDisappeared("Not confirmed by original runtime")

            monkeypatch.setattr(item.task.control.host_processes, "read", read)
        with pytest.raises(SandboxUnavailable):
            item.monitor.observe_termination(item.task.service)

    def test_new_foreign_process_after_confirmed_stop_is_not_idle(self, combined, monkeypatch):
        item = combined
        item.monitor.observe(item.task.service)
        calls = []

        def read():
            calls.append(1)
            stopped(item)
            value = copy.deepcopy(item.value)
            if len(calls) > 1:
                value["pids"]["inventory"] = [9999]
            return value

        monkeypatch.setattr(item.monitor, "read", read)
        with pytest.raises(SandboxUnavailable, match="occupied"):
            item.monitor.observe_termination(item.task.service)

    def test_exit_mode_never_binds_new_observer_or_switches_task(self, combined):
        item = combined
        with pytest.raises(SandboxUnavailable):
            item.monitor.observe_termination(item.task.service)
        item.monitor.observe(item.task.service)
        with pytest.raises(SandboxUnavailable):
            item.monitor.observe_termination(None)


def test_regular_task_cannot_request_maintenance_exit_observation(combined):
    combined.monitor.observe(combined.task.service)
    with pytest.raises(SandboxUnavailable, match="maintenance"):
        combined.monitor.observe_termination(combined.task.service)
