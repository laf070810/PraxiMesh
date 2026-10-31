"""Actual fixed workload probes, used by container_compiler_acceptance."""

import time
import uuid

from judge.container_compiler import CompilerContainerService
from judge.container_connection import ContainerConnection
from judge.container_contract import PYTHON, WORKLOAD_PROFILE, resource_limits
from judge.sandbox import SandboxUnavailable
from scripts.container_journal_acceptance import save
from scripts.container_supervision_acceptance import gone, tree


def run_resource(engine, image, journal, name, report, output):
    if name not in {"cpu", "memory", "storage", "pids"}:
        raise ValueError("Unknown fixed resource probe")
    service = CompilerContainerService(
        journal, uuid.uuid4(), uuid.uuid4(), image=image, resource_profile=WORKLOAD_PROFILE
    )
    connection = None
    processes = {}
    record = {"name": "resource-" + name, "passed": False, "resource_profile": WORKLOAD_PROFILE}
    report["checks"].append(record)
    save(output / "result.json", report)
    try:
        status = service.start(
            [PYTHON, "-I", "/opt/praximesh/container_workload_fixture.py", name],
            deadline=time.monotonic() + 80,
            on_tick=lambda: None,
        )
        assert status["limits"] == resource_limits(WORKLOAD_PROFILE)
        record.update(container=service.container_id, supervision=status)
        connection = ContainerConnection(service, status, on_tick=lambda: None)
        channel = connection.channel
        channel.send({"event": "prepare-resource", "name": name})
        ready = channel.receive()
        assert ready["event"] == "resource-ready" and ready["name"] == name
        assert ready["resource_profile"] == WORKLOAD_PROFILE
        assert ready["limits"] == status["limits"]
        if name != "pids":
            assert ready["guest"]["gpu_nodes"] == [] and ready["guest"]["root_write"] is False
        record["ready"] = ready
        processes = tree(engine, service)
        record["processes"] = processes
        channel.send({"event": "start"})
        try:
            value = channel.receive()
        except SandboxUnavailable:
            state = service.inspect()
            record["disconnected_state"] = state
            until = min(status["deadline"], time.monotonic() + 2)
            while (
                name == "memory"
                and state is not None
                and state["running"]
                and time.monotonic() < until
            ):
                time.sleep(0.05)
                state = service.inspect()
            record["final_disconnected_state"] = state
            if name != "memory" or state is None or state["running"] or not state["oom_killed"]:
                raise
            # Whole-task OOM can kill PID 1; preserve the original daemon evidence.
            record["result"] = {"outer_oom_killed": True, "exit_code": state["exit_code"]}
        else:
            record["result"] = value
            if value.get("event") == "resource-failed":
                channel.send({"event": "acknowledged"})
                raise RuntimeError(
                    "Fixed resource probe failed: "
                    + value["failure_kind"]
                    + ": "
                    + value["failure"]
                )
            assert value["event"] == "resource-checked" and value["name"] == name
            assert value["passed"] is True and value["ready"] is False
            assert value["gpu_executed"] is False and value["resource_profile"] == WORKLOAD_PROFILE
            assert value["cleanup"] == (
                [] if name == "pids" else [{"id": "resource", "stopped": True, "removed": True}]
            )
            details = value["details"]
            if name == "cpu":
                assert details["cpu_seconds"] >= 0.5 and details["elapsed_seconds"] >= 3
                assert details["cpu_seconds"] / details["elapsed_seconds"] <= 0.7
                assert details["throttled_periods"] > 0
            elif name == "storage":
                assert details["limited"] is True and 1 <= details["mib"] <= 32
            elif name == "memory":
                assert details["oom_kills"] > 0 and details["classification"] == "memory_limit"
            else:
                assert details["limited"] is True and details["limit_events"] > 0
                assert 1 <= details["children"] < 256 and details["peak_tasks"] == 256
                assert details["remaining_tasks"] < 32
            record["result"] = value
            channel.send({"event": "acknowledged"})
            connection.finish()
        record["passed"] = True
    except BaseException as error:
        record["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            try:
                if connection is not None:
                    connection.close()
            finally:
                record["original_record"] = journal.load()
                cleanup = journal.recover()
                if cleanup is not None:
                    report["cleanup"].append(cleanup)
                gone(processes)
                record["process_tree_gone"] = True
        except BaseException as error:
            record.update(passed=False, cleanup_failure=type(error).__name__ + ": " + str(error))
            report["retain_storage"] = True
            raise
        finally:
            save(output / "result.json", report)
