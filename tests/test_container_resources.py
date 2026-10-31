"""Budget/record refusal and kernel evidence boundaries; actual limits use CPU probes."""

import json
import uuid
from types import SimpleNamespace

import pytest

from judge import container_oci, container_resources
from judge.container_compiler import CompilerContainerService
from judge.container_contract import (
    SUPERVISION_PROFILE,
    WORKLOAD_PROFILE,
    docker_resources,
    marker_profile,
    resource_limits,
    resource_marker,
)
from judge.container_journal import RecoveryContainer
from judge.container_service import PREFIX, digest
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_supervision import IMAGE, snapshot


def intent(journal, profile):  # noqa: F811
    service = CompilerContainerService(
        journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE, resource_profile=profile
    )
    service.image_env = []
    state = snapshot(service)
    args = state["Config"]["Cmd"]
    args[3] = resource_marker(profile)
    state["Config"]["Labels"].update(service.required_image_labels())
    state["Config"]["Labels"][PREFIX + "command"] = digest(args)
    state["HostConfig"].update(docker_resources(profile))
    service.record_intent(args)
    state["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(journal.load()["security"][1]),
    ]
    journal.engine.state = state
    return service, state


@pytest.mark.parametrize("profile", [None, False, {}, "unbounded", "0.5", "container-workload-v2"])
def test_unknown_or_user_supplied_budgets_cannot_create_a_service(journal, profile):  # noqa: F811
    with pytest.raises(ValueError, match="resource profile"):
        CompilerContainerService(
            journal, uuid.uuid4(), uuid.uuid4(), image=IMAGE, resource_profile=profile
        )
    assert journal.load() is None and journal.engine.state is None


@pytest.mark.parametrize("profile,version", [(SUPERVISION_PROFILE, 1), (WORKLOAD_PROFILE, 2)])
def test_recovery_uses_the_recorded_budget_after_source_upgrade(journal, profile, version):  # noqa: F811
    service, state = intent(journal, profile)
    original = journal.load()
    assert original["version"] == version
    assert original.get("resource_profile", SUPERVISION_PROFILE) == profile
    # Frozen image contract can differ from current source without executing it.
    original["labels"][PREFIX + "runtime"] = "f" * 64
    state["Config"]["Labels"].update(original["labels"])
    journal.write(journal.filename, original)
    restored = RecoveryContainer(journal, journal.load())
    assert restored.resource_profile == profile
    with pytest.raises(SandboxUnavailable, match="cannot execute"):
        restored.start([], deadline=0, on_tick=lambda: None)
    result = journal.recover()
    assert result["removed"] is True and result["ready"] is False
    assert journal.engine.state is None and journal.load() is None
    archived = json.loads((journal.directory / result["history"]).read_text())
    assert archived["version"] == version and archived["arguments"] == original["arguments"]
    assert not any(
        call[:2] in {("container", "start"), ("container", "exec")} for call in journal.engine.calls
    )


@pytest.mark.parametrize(
    "defect", ["missing", "legacy", "unknown", "boolean", "marker", "version", "extra"]
)
def test_incomplete_or_mismatched_v2_records_never_authorize_cleanup(journal, defect):  # noqa: F811
    intent(journal, WORKLOAD_PROFILE)
    record = journal.load()
    if defect == "missing":
        record.pop("resource_profile")
    elif defect == "legacy":
        record["resource_profile"] = SUPERVISION_PROFILE
    elif defect == "unknown":
        record["resource_profile"] = "unbounded"
    elif defect == "boolean":
        record["resource_profile"] = True
    elif defect == "marker":
        record["arguments"][3] = "--"
    elif defect == "version":
        record["version"] = 1
    else:
        record["memory_bytes"] = 1024**3
    journal.write(journal.filename, record)
    with pytest.raises(SandboxUnavailable, match="durable container"):
        journal.recover()
    assert journal.read(journal.filename) == record
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")} for call in journal.engine.calls
    )


@pytest.mark.parametrize(
    "field,value",
    [("CpuQuota", 200000), ("Memory", 2147483648), ("MemorySwap", 1073741824), ("PidsLimit", 512)],
)
def test_actual_container_limits_must_match_saved_task_budget(journal, field, value):  # noqa: F811
    service, state = intent(journal, WORKLOAD_PROFILE)
    assert service.inspect()["running"] is True
    state["HostConfig"][field] = value
    with pytest.raises(SandboxUnavailable, match="containment"):
        journal.recover()
    assert journal.load()["phase"] == "intent"
    assert not any(
        call[:2] in {("container", "kill"), ("container", "rm")} for call in journal.engine.calls
    )


@pytest.mark.parametrize(
    "marker",
    [
        "--profile=unlimited",
        "--resource-profile=container-supervision-v1",
        "--resource-profile=0.5",
        None,
    ],
)
def test_supervisor_accepts_only_versioned_launch_markers(marker):
    with pytest.raises(ValueError, match="resource marker"):
        marker_profile(marker)


def test_known_markers_bind_the_frozen_kernel_budget():
    assert marker_profile("--") == SUPERVISION_PROFILE
    assert marker_profile(resource_marker(WORKLOAD_PROFILE)) == WORKLOAD_PROFILE
    assert resource_limits(WORKLOAD_PROFILE) == {
        "cpu.max": "50000 100000",
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "256",
    }
    assert docker_resources(SUPERVISION_PROFILE)["Memory"] == 2147483648


@pytest.mark.parametrize(
    "ooms,kills,code,expected",
    [
        (0, 0, 137, None),
        (0, 1, 137, None),
        (1, 0, 137, None),
        (1, 1, None, None),
        (1, 1, 137, "memory_limit"),
    ],
)
def test_oom_classification_needs_kernel_event_and_verified_cleanup(
    monkeypatch, ooms, kills, code, expected
):
    runtime = container_oci.ContainerRuntime.__new__(container_oci.ContainerRuntime)
    counts = iter(
        [
            {"max": 0, "oom": 0, "oom_kill": 0},
            {"max": ooms, "oom": ooms, "oom_kill": kills},
        ]
    )
    cleanup = []
    monkeypatch.setattr(container_oci, "counters", lambda _name: next(counts))
    monkeypatch.setattr(runtime, "attach", lambda _label: SimpleNamespace(poll=lambda: code))
    monkeypatch.setattr(
        runtime, "inspect", lambda _label: pytest.fail("Do not query absent OOM state")
    )
    monkeypatch.setattr(runtime, "stop_container", lambda label: cleanup.append(label))
    monkeypatch.setattr(
        container_oci,
        "GPUSession",
        lambda _process, callback, **kwargs: {"cleanup": callback, **kwargs},
    )
    session = runtime.session("resource", mode="submit", on_tick=lambda: None)
    assert session["exit_verdict"](137) == expected
    assert cleanup == (["resource"] if expected else [])
    session["cleanup"]()
    assert cleanup == ["resource"]


def test_oom_cleanup_failure_cannot_become_a_memory_verdict(monkeypatch):
    runtime = container_oci.ContainerRuntime.__new__(container_oci.ContainerRuntime)
    counts = iter([{"max": 0, "oom": 0, "oom_kill": 0}, {"max": 1, "oom": 1, "oom_kill": 1}])
    monkeypatch.setattr(container_oci, "counters", lambda _name: next(counts))
    monkeypatch.setattr(runtime, "attach", lambda _label: SimpleNamespace(poll=lambda: 137))

    def stop(_label):
        raise SandboxUnavailable("Unconfirmed owned process cleanup")

    monkeypatch.setattr(runtime, "stop_container", stop)
    monkeypatch.setattr(container_oci, "GPUSession", lambda *_args, **kwargs: kwargs)
    session = runtime.session("resource", mode="submit", on_tick=lambda: None)
    with pytest.raises(SandboxUnavailable, match="Unconfirmed"):
        session["exit_verdict"](137)


def test_live_attach_client_cannot_hide_an_oom_until_the_request_times_out(monkeypatch):
    from judge.gpu_session import GPUExecutionError

    runtime = container_oci.ContainerRuntime.__new__(container_oci.ContainerRuntime)
    counts = iter([{"max": 0, "oom": 0, "oom_kill": 0}, {"max": 10, "oom": 1, "oom_kill": 1}])
    events = []
    monkeypatch.setattr(container_oci, "counters", lambda _name: next(counts))
    monkeypatch.setattr(runtime, "attach", lambda _label: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(runtime, "stop_container", lambda label: events.append("stopped " + label))
    monkeypatch.setattr(
        container_oci,
        "GPUSession",
        lambda _process, callback, **kwargs: {"cleanup": callback, **kwargs},
    )
    session = runtime.session(
        "resource", mode="submit", on_tick=lambda: events.append("ownership checked")
    )
    with pytest.raises(GPUExecutionError) as error:
        session["on_tick"]()
    assert error.value.verdict == "memory_limit"
    session["cleanup"]()
    assert events == ["ownership checked", "stopped resource"]


@pytest.mark.parametrize(
    "data",
    [
        "oom_kill 1\n",
        "max 1\noom 2\noom_kill -1\n",
        "max 1\noom 2\noom_kill 1\noom_kill 2\n",
        "max 1\noom 2\noom_kill １\n",
        "x" * 8193,
    ],
)
def test_invalid_kernel_evidence_is_not_interpreted_as_an_oom(tmp_path, monkeypatch, data):
    monkeypatch.setattr(container_resources, "GROUP", tmp_path)
    (tmp_path / "memory.events").write_text(data)
    with pytest.raises(SandboxUnavailable):
        container_resources.counters("memory.events")


def test_fixed_native_pid_probe_refuses_the_developer_host(monkeypatch):
    monkeypatch.setattr(container_resources.os, "getuid", lambda: 2031)
    monkeypatch.setattr(container_resources.os, "fork", lambda: pytest.fail("Host fork attempted"))
    with pytest.raises(SandboxUnavailable, match="private bounded"):
        container_resources.host_pid_probe()


def test_resource_probes_reject_the_development_profile_before_docker(monkeypatch):
    from scripts import container_compiler_acceptance as command

    monkeypatch.setattr(command, "LocalEngine", lambda *_a: pytest.fail("Docker reached"))
    monkeypatch.setattr(
        command.sys,
        "argv",
        [
            "probe",
            "--docker-socket",
            "/unused",
            "--runtime-archive",
            "/unused",
            "--nvrtc-archive",
            "/unused",
            "--resource-probes",
        ],
    )
    with pytest.raises(SystemExit) as error:
        command.main()
    assert error.value.code == 2
