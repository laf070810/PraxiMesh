"""Reject incomplete resource reports; synthetic values do not prove isolation."""

import copy
import hashlib
from types import SimpleNamespace

import pytest

from judge import gpu_host_pids
from judge.gpu_isolation import (
    CHECKS,
    CONTAINERS,
    check_resources,
    request_values,
    validate_report,
    verify_identity,
)
from judge.sandbox import SandboxUnavailable

REQUEST = {"canary": "a" * 32, "port": 12345}
TARGET = {
    "index": 0,
    "uuid": "GPU-11111111-2222-3333-4444-555555555555",
    "model": "synthetic device",
    "driver": "590.48.01",
    "architecture": [8, 6],
}


def identity(target=None):
    return {
        "event": "identity",
        "uid": 65534,
        "root_write": False,
        "private_file_visible": False,
        "host_loopback_reachable": False,
        "gpu_nodes": sorted(["nvidia0", "nvidiactl", "nvidia-uvm"]) if target else [],
        "devices": [[target["uuid"], target["model"], target["driver"]]] if target else [],
        "capabilities": "0000000000000000",
        "environment_names": ["PATH"],
        "sha256": hashlib.sha256(REQUEST["canary"].encode()).hexdigest(),
    }


def resource_report(request=REQUEST, target=None):
    names = CONTAINERS
    return {
        "gpu": target["uuid"] if target else None,
        "canary_sha256": hashlib.sha256(request["canary"].encode()).hexdigest(),
        "checks": [
            {
                "name": name,
                "passed": True,
                "cpu_recovery": True,
                "details": {"scope": "synthetic unit-test report"},
            }
            for name in CHECKS
        ],
        "cleanup": [
            {
                "name": name,
                "id": f"{index:064x}",
                "running_before_cleanup": False,
                "exit_code": 0,
                "stopped": True,
            }
            for index, name in enumerate(names)
        ],
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"canary": "a", "port": 12345},
        {"canary": "a" * 32, "port": True},
        {"canary": "a" * 32, "port": 0},
        {"canary": "a" * 32, "port": 65536},
        dict(REQUEST, source="untrusted"),
    ],
)
def test_request_rejects_source_unbounded_port_or_missing_challenge(value):
    with pytest.raises(ValueError):
        request_values(value)


@pytest.mark.parametrize("target", [None, TARGET])
def test_known_identity_and_complete_synthetic_report_have_expected_shape(target):
    verify_identity(identity(target), REQUEST, target)
    validate_report(resource_report(target=target), REQUEST, target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", True),
        ("uid", 0),
        ("root_write", True),
        ("private_file_visible", True),
        ("host_loopback_reachable", True),
        ("host_loopback_reachable", 0),
        ("capabilities", "0001"),
        ("environment_names", ["DATABASE_URL"]),
        ("environment_names", ["DOCKER_HOST"]),
        ("environment_names", ["HTTPS_PROXY"]),
        ("sha256", "b" * 64),
        ("gpu_nodes", ["nvidia0"]),
        ("devices", [["unknown"]]),
    ],
)
def test_isolation_leaks_and_false_boolean_reports_are_rejected(field, value):
    report = identity()
    report[field] = value
    with pytest.raises(SandboxUnavailable):
        verify_identity(report, REQUEST, None)


def test_gpu_check_cannot_use_cpu_identity_or_another_device():
    with pytest.raises(SandboxUnavailable):
        verify_identity(identity(), REQUEST, TARGET)
    other = identity(TARGET)
    other["devices"][0][0] = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with pytest.raises(SandboxUnavailable):
        verify_identity(other, REQUEST, TARGET)
    with pytest.raises(SandboxUnavailable):
        validate_report(resource_report(), REQUEST, TARGET)


@pytest.mark.parametrize("target,expect_gpu", [(None, True), (TARGET, False), (None, 0)])
def test_cpu_replay_cannot_satisfy_gpu_resource_mode(tmp_path, target, expect_gpu):
    runtime = SimpleNamespace(base=tmp_path, target=target)
    with pytest.raises(SandboxUnavailable, match="device mode"):
        check_resources(runtime, REQUEST, expect_gpu=expect_gpu, record={})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("uid", [0, 1])
def test_native_pid_probe_refuses_an_unowned_namespace_before_forking(monkeypatch, uid):
    monkeypatch.setattr(gpu_host_pids.os, "getuid", lambda: uid)
    monkeypatch.setattr(
        gpu_host_pids,
        "Path",
        lambda _name: SimpleNamespace(read_text=lambda: "0::/not-the-controller"),
    )
    monkeypatch.setattr(gpu_host_pids.os, "fork", lambda: pytest.fail("must not fork"))
    with pytest.raises(RuntimeError, match="private workload cgroup"):
        gpu_host_pids.main()


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "reordered",
        "failed",
        "boolean",
        "cleanup",
        "live",
        "reused",
        "extra",
        "stale-challenge",
    ],
)
def test_resource_report_cannot_omit_checks_or_cleanup(change):
    report = copy.deepcopy(resource_report())
    if change == "partial":
        report["checks"].pop()
    elif change == "reordered":
        report["checks"].reverse()
    elif change == "failed":
        report["checks"][0]["passed"] = False
    elif change == "boolean":
        report["checks"][0]["passed"] = 1
    elif change == "cleanup":
        report["cleanup"].pop()
    elif change == "live":
        report["cleanup"][0]["stopped"] = False
    elif change == "reused":
        report["cleanup"][1]["id"] = report["cleanup"][0]["id"]
    elif change == "extra":
        report["result"] = {"verdict": "accepted"}
    else:
        report["canary_sha256"] = "b" * 64
    with pytest.raises(SandboxUnavailable):
        validate_report(report, REQUEST, None)
