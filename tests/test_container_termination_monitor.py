"""Maintenance-only exit races with original state; no hardware evidence."""

import copy
import math
from types import SimpleNamespace

import pytest

from judge import container_processes as process_module
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
    @pytest.fixture(autouse=True)
    def original_deadline_clock(self, combined, monkeypatch):
        hard = float(combined.task.journal.load()["arguments"][1])
        combined.clock = SimpleNamespace(now=hard - 1, hard=hard)
        monkeypatch.setattr(process_module.time, "monotonic", lambda: combined.clock.now)
        monkeypatch.setattr(
            process_module.time,
            "sleep",
            lambda delay: setattr(combined.clock, "now", combined.clock.now + delay),
        )

    def test_current_sample_retains_owned_member_across_original_exit(self, combined, monkeypatch):
        item = combined
        item.monitor.observe(item.task.service)
        calls = []

        def read(*, on_sample=None):
            value = copy.deepcopy(item.value)
            calls.append(1)
            if len(calls) == 1:
                value["pids"]["inventory"] = [5678]
                if on_sample is not None:
                    on_sample([5678])
                stopped(item)
            elif on_sample is not None:
                on_sample([])
            return value

        monkeypatch.setattr(item.monitor, "read", read)
        assert item.monitor.observe_termination(item.task.service) == [5678]
        assert len(calls) == 2

    def test_query_exit_preserves_anchor_and_requeries_after_original_stop(
        self, combined, monkeypatch
    ):
        item = combined
        item.monitor.observe(item.task.service)
        original = item.monitor.owned
        calls = []

        def read(*, on_sample=None):
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

            def read(*, on_sample=None):
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

        def read(*, on_sample=None):
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

    def test_bound_original_exit_waits_for_runtime_without_requerying_gpu(
        self, combined, monkeypatch
    ):
        item = combined
        item.monitor.observe(item.task.service)
        queries = []
        inspect = item.task.service.inspect

        def delayed():
            if item.clock.now >= item.clock.hard + 0.6:
                stopped(item)
            return inspect()

        def read(*, on_sample=None):
            queries.append(item.clock.now)
            value = copy.deepcopy(item.value)
            if len(queries) == 1:
                on_sample([5678])
                value["pids"]["inventory"] = [5678]
                item.task.exited_pids = {1234, 5678}
                del item.task.members[1234]
                del item.task.members[5678]
                item.clock.now = item.clock.hard + 0.1
            else:
                on_sample([])
            return value

        monkeypatch.setattr(item.task.service, "inspect", delayed)
        monkeypatch.setattr(item.monitor, "read", read)
        assert item.monitor.observe_termination(item.task.service) == [5678]
        assert len(queries) == 2 and queries[1] >= item.clock.hard + 0.6
        assert item.clock.now < item.clock.hard + 5

    @pytest.mark.parametrize("positive", [False, True])
    def test_unbound_missing_root_requires_confirmed_stop_before_any_inventory(
        self, combined, monkeypatch, positive
    ):
        item = combined
        item.monitor.observe(item.task.service)
        del item.task.members[1234]
        inspect = item.task.service.inspect
        queries = []

        def delayed():
            if item.clock.now >= item.clock.hard:
                stopped(item)
            return inspect()

        def read(*, on_sample=None):
            assert not item.task.state["State"]["Running"]
            queries.append(item.clock.now)
            value = copy.deepcopy(item.value)
            value["pids"]["inventory"] = [5678] if positive else []
            on_sample(value["pids"]["inventory"])
            return value

        monkeypatch.setattr(item.task.service, "inspect", delayed)
        monkeypatch.setattr(item.monitor, "read", read)
        if positive:
            with pytest.raises(SandboxUnavailable, match="occupied"):
                item.monitor.observe_termination(item.task.service)
        else:
            assert item.monitor.observe_termination(item.task.service) == []
        assert len(queries) == 1 and queries[0] >= item.clock.hard

    @pytest.mark.parametrize("kind", ["extended", "expired", "boolean", "nan", "infinity"])
    def test_termination_cannot_extend_or_refresh_original_deadline(self, combined, kind):
        item = combined
        item.monitor.observe(item.task.service)
        deadline = {
            "extended": math.nextafter(item.clock.hard + 5, math.inf),
            "expired": item.clock.now,
            "boolean": True,
            "nan": math.nan,
            "infinity": math.inf,
        }[kind]
        with pytest.raises(SandboxUnavailable, match="deadline"):
            item.monitor.observe_termination(item.task.service, deadline=deadline)


def test_regular_task_cannot_request_maintenance_exit_observation(combined):
    combined.monitor.observe(combined.task.service)
    with pytest.raises(SandboxUnavailable, match="maintenance"):
        combined.monitor.observe_termination(combined.task.service)
