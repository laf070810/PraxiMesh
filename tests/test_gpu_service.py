"""Service ownership rejection tests; real process checks live in acceptance."""

import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from judge import gpu_service
from judge.gpu_device_lock import DeviceLock
from judge.gpu_service import OwnedJobService, duration_seconds
from judge.sandbox import SandboxUnavailable


@pytest.fixture
def service(tmp_path):
    with DeviceLock(tmp_path / "locks", "GPU-11111111-2222-3333-4444-555555555555") as guard:
        yield OwnedJobService(guard, uuid.uuid4(), uuid.uuid4())


def test_recovery_has_a_distinct_stable_identity_for_the_same_reservation(service):
    recovery = OwnedJobService(
        service.guard, service.job_id, service.reservation_token, purpose="recovery"
    )
    other = OwnedJobService(
        service.guard, service.job_id, service.reservation_token, purpose="recovery"
    )
    assert recovery.unit == other.unit and recovery.unit != service.unit
    assert recovery.description != service.description
    assert recovery.unit.startswith("praximesh-gpu-recovery-")
    assert (
        recovery.job_id == service.job_id
        and recovery.reservation_token == service.reservation_token
    )
    with pytest.raises(ValueError, match="purpose"):
        OwnedJobService(
            service.guard, service.job_id, service.reservation_token, purpose="arbitrary"
        )


def snapshot(service):
    return {
        "Id": service.unit,
        "Description": service.description,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "Transient": "yes",
        "Type": "exec",
        "Restart": "no",
        "KillMode": "control-group",
        "KillSignal": "9",
        "FinalKillSignal": "9",
        "SendSIGKILL": "yes",
        "TimeoutStartUSec": "5s",
        "TimeoutStopUSec": "5s",
        "RuntimeMaxUSec": "1min 10s",
        "RuntimeRandomizedExtraUSec": "0",
        "ControlGroup": service.cgroup_name,
        "InvocationID": "a" * 32,
        "MainPID": "1",
        "Delegate": "yes",
        "CPUQuotaPerSecUSec": "2s",
        "MemoryMax": "2147483648",
        "MemorySwapMax": "0",
        "TasksMax": "512",
        "StandardOutput": "null",
        "StandardError": "null",
    }


@pytest.mark.parametrize(
    "value,seconds", [("0", 0), ("5s", 5), ("1min 1.25s", 61.25), ("200ms", 0.2), ("1us", 0.000001)]
)
def test_service_time_format(value, seconds):
    assert duration_seconds(value) == seconds


@pytest.mark.parametrize(
    "value", ["infinity", "NaN", "-1s", "1", "1s garbage", "1s1s", " 1s", "1fortnight"]
)
def test_unknown_or_unbounded_service_times_fail_closed(value):
    with pytest.raises(SandboxUnavailable):
        duration_seconds(value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("Description", "someone else's service"),
        ("Transient", "no"),
        ("KillMode", "process"),
        ("KillSignal", "15"),
        ("RuntimeMaxUSec", "infinity"),
        ("RuntimeMaxUSec", "1h"),
        ("Restart", "always"),
        ("Type", "notify"),
        ("InvocationID", ""),
        ("ControlGroup", "/user.slice/another.service"),
        ("StandardError", "journal"),
        ("RuntimeRandomizedExtraUSec", "5s"),
    ],
)
def test_mismatched_service_is_never_stopped(service, monkeypatch, key, value):
    state = snapshot(service)
    state[key] = value
    calls = []

    def command(program, *arguments):
        calls.append((program, arguments))
        assert "show" in arguments
        return SimpleNamespace(
            returncode=0, stdout="\n".join(k + "=" + v for k, v in state.items())
        )

    monkeypatch.setattr(service, "_command", command)
    with pytest.raises(SandboxUnavailable):
        service.stop()
    assert len(calls) == 1


def test_same_name_with_new_invocation_is_not_the_observed_task(service, monkeypatch):
    state = snapshot(service)
    monkeypatch.setattr(
        service,
        "_command",
        lambda *args: SimpleNamespace(
            returncode=0, stdout="\n".join(k + "=" + v for k, v in state.items())
        ),
    )
    assert service.inspect()["InvocationID"] == "a" * 32
    state["InvocationID"] = "b" * 32
    with pytest.raises(SandboxUnavailable, match="invocation changed"):
        service.stop()


def test_reservation_identity_is_stable_for_recovery_but_not_reused_for_next_task(service):
    current = OwnedJobService(service.guard, service.job_id, service.reservation_token)
    changed = OwnedJobService(service.guard, service.job_id, uuid.uuid4())
    assert current.unit == service.unit and current.description == service.description
    assert changed.unit != service.unit


def test_absent_unit_is_not_cleanup_if_its_cgroup_is_populated(service, monkeypatch):
    monkeypatch.setattr(service, "inspect", lambda: None)
    monkeypatch.setattr(service, "populated", lambda: True)
    monkeypatch.setattr(gpu_service, "STOP_SECONDS", 0)
    monkeypatch.setattr(gpu_service, "CONTROL_SECONDS", 0)
    with pytest.raises(SandboxUnavailable, match="still has running processes"):
        service.stop()


@pytest.mark.parametrize("after", ["absent", "inactive", "running", "leftover", "replacement"])
def test_failed_stop_requires_fresh_identity_and_empty_cgroup(service, monkeypatch, after):
    state = snapshot(service)
    calls = []

    def command(program, *arguments):
        calls.append(arguments)
        if "stop" in arguments:
            if after in {"absent", "leftover"}:
                state.clear()
                state.update(Id=service.unit, LoadState="not-found")
            elif after == "inactive":
                state.update(ActiveState="inactive", MainPID="0", ControlGroup="")
            elif after == "replacement":
                state["InvocationID"] = "b" * 32
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(
            returncode=0, stdout="\n".join(k + "=" + v for k, v in state.items())
        )

    monkeypatch.setattr(service, "_command", command)
    monkeypatch.setattr(service, "populated", lambda: after == "leftover")
    if after in {"absent", "inactive"}:
        assert service.stop()["cgroup_empty"]
    else:
        with pytest.raises(SandboxUnavailable):
            service.stop()
    assert sum("stop" in arguments for arguments in calls) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("cpu.max", "max 100000"),
        ("cpu.max", "400000 100000"),
        ("memory.max", "max"),
        ("memory.swap.max", "max"),
        ("pids.max", "max"),
    ],
)
def test_service_properties_do_not_replace_actual_parent_limits(service, tmp_path, key, value):
    group = tmp_path / "cgroup"
    group.mkdir()
    service.cgroup = group
    limits = {
        "cpu.max": "200000 100000",
        "memory.max": "2147483648",
        "memory.swap.max": "0",
        "pids.max": "512",
    }
    for name, current in limits.items():
        (group / name).write_text(current)
    assert service.limits() == limits
    (group / key).write_text(value)
    with pytest.raises(SandboxUnavailable):
        service.limits()


def test_waiting_callback_cannot_start_a_task_after_its_reserved_window(service, monkeypatch):
    clock = [0]
    monkeypatch.setattr(gpu_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "inspect", lambda: None)
    monkeypatch.setattr(service, "populated", lambda: False)
    monkeypatch.setattr(service, "_command", lambda *args: pytest.fail("No service may be started"))

    def tick():
        clock[0] = 3

    with pytest.raises(SandboxUnavailable, match="expired before"):
        service.start([sys.executable], deadline=12, on_tick=tick)


def test_rounding_a_tiny_remaining_budget_cannot_disable_service_timer(service, monkeypatch):
    monkeypatch.setattr(gpu_service.time, "monotonic", lambda: 0)
    monkeypatch.setattr(service, "inspect", lambda: None)
    monkeypatch.setattr(service, "populated", lambda: False)
    monkeypatch.setattr(service, "_command", lambda *args: pytest.fail("No service may be started"))
    with pytest.raises(SandboxUnavailable, match="finite service timer"):
        service.start([sys.executable], deadline=10.0000005, on_tick=lambda: None)


@pytest.mark.parametrize("expired", [False, True])
def test_real_launch_gate_refuses_a_late_service_before_exec(tmp_path, expired):
    gate = Path(__file__).resolve().parents[1] / "scripts/gpu_service_entry.py"
    marker = tmp_path / "started"
    result = subprocess.run(
        [
            sys.executable,
            str(gate),
            "--start-before",
            str(time.monotonic() + (-1 if expired else 5)),
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == (70 if expired else 0)
    assert marker.exists() is not expired
    assert not result.stdout and not result.stderr
