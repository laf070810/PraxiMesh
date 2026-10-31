"""Modeled report refusal; real resource evidence requires the CPU/runsc entry."""

import copy
import hashlib

import pytest

from judge import container_isolation_protocol as protocol
from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_kernel_protocol import PROTOCOL, checked_completion, expected_cleanup
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_adapter import TARGET

REQUEST = protocol.resource_request("a" * 32)
SELECTION = {
    "schema": 1,
    "profile": "single-cuda-device-v1",
    "uuid": TARGET["uuid"],
    "driver": TARGET["driver"],
    "minor": 2,
    "uvm_major": 509,
}


def resource_report(request=REQUEST, *, gpu=False):
    target, selection = (TARGET, SELECTION) if gpu else (None, None)
    cuda, nodes = protocol.target_identity(target, selection)
    canary = hashlib.sha256(request["canary"].encode()).hexdigest()
    environment = ["PATH", "PYTHONDONTWRITEBYTECODE", "HOME", "LANG", "LC_CTYPE"]
    if gpu:
        environment += ["NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES"]
    details = {
        "identity": {
            "guest": {
                "event": "identity",
                "uid": 0,
                "root_write": False,
                "private_file_visible": False,
                "controller_loopback_reachable": False,
                "gpu_nodes": nodes,
                "cuda": cuda,
                "capabilities": "0000000000000000",
                "no_new_privileges": "1",
                "environment_names": sorted(environment),
                "sha256": canary,
            }
        },
        "output": {"verdict": "output_limit"},
        "timeout": {"verdict": "time_limit"},
        "cpu-limit": {"elapsed_seconds": 3.1, "cpu_seconds": 1.5, "throttled_periods": 30},
        "storage-limit": {"event": "storage", "limited": True, "mib": 32},
        "memory-limit": {"verdict": "memory_limit", "oom_kills": 1},
        "pid-limit": {
            "limited": True,
            "children": 250,
            "limit_events": 1,
            "peak_tasks": 256,
            "remaining_tasks": 6,
            "initial_tasks": 6,
        },
    }
    for name in ("cancel", "client-exit"):
        details[name] = {
            "interruption": name,
            "observation": {
                "scope": "runsc-guest-v1",
                "id": name,
                "status": "running",
                "attach_alive": True,
            },
        }
    return {
        "scope": protocol.SCOPE,
        "gpu": TARGET["uuid"] if gpu else None,
        "canary_sha256": canary,
        "resource_profile": WORKLOAD_PROFILE,
        "limits": resource_limits(WORKLOAD_PROFILE),
        "checks": [
            {"name": name, "passed": True, "cpu_recovery": True, "details": details[name]}
            for name in protocol.CHECKS
        ],
        "cleanup": [
            {
                "scope": "runsc-guest-v1",
                "id": name,
                "running_before_cleanup": True,
                "exit_code": 0,
                "stopped": True,
                "removed": True,
            }
            for name in protocol.LABELS
        ],
    }


def completion(request=REQUEST, *, gpu=False):
    return {
        "event": "kernel-completed",
        "protocol": PROTOCOL,
        "recovered": True,
        "ready": False,
        "cleanup": expected_cleanup(request),
        "report": resource_report(request, gpu=gpu),
    }


@pytest.mark.parametrize("gpu", [False, True])
def test_separate_cpu_and_gpu_reports_bind_original_device_and_budget(gpu):
    value = completion(gpu=gpu)
    kwargs = {"target": TARGET, "selection": SELECTION} if gpu else {}
    assert checked_completion(value, REQUEST, **kwargs) == value
    assert len(value["report"]["cleanup"]) == 17
    assert "result" not in value
    if gpu:
        with pytest.raises(SandboxUnavailable):
            checked_completion(value, REQUEST)
    else:
        with pytest.raises(SandboxUnavailable):
            checked_completion(value, REQUEST, target=TARGET, selection=SELECTION)


@pytest.mark.parametrize("extra", ["port", "protected_path", "source", "command", "limits", "gpu"])
def test_caller_cannot_supply_probe_paths_commands_or_budgets(extra):
    with pytest.raises(ValueError):
        protocol.validate_request({**REQUEST, extra: "untrusted"})


@pytest.mark.parametrize("canary", [None, True, 1, "", "a" * 31, "a" * 33, "A" * 32, "x" * 32])
def test_invalid_challenges_fail_before_starting(canary):
    with pytest.raises(ValueError):
        protocol.resource_request(canary)


@pytest.mark.parametrize(
    "path,value",
    [
        (("scope",), "host-resource-report"),
        (("canary_sha256",), "b" * 64),
        (("resource_profile",), "guest-only"),
        (("limits", "pids.max"), 256),
        (("gpu",), None),
        (("checks", 0, "passed"), 1),
        (("checks", 0, "cpu_recovery"), False),
        (("checks", 0, "details", "guest", "uid"), False),
        (("checks", 0, "details", "guest", "uid"), 65534),
        (("checks", 0, "details", "guest", "root_write"), True),
        (("checks", 0, "details", "guest", "private_file_visible"), True),
        (("checks", 0, "details", "guest", "controller_loopback_reachable"), True),
        (("checks", 0, "details", "guest", "capabilities"), "0000000000000001"),
        (("checks", 0, "details", "guest", "no_new_privileges"), "0"),
        (("checks", 0, "details", "guest", "environment_names"), ["DATABASE_URL"]),
        (("checks", 0, "details", "guest", "cuda", "count"), True),
        (("checks", 0, "details", "guest", "cuda", "uuid"), "GPU-" + "b" * 36),
        (("checks", 0, "details", "guest", "cuda", "architecture"), [7, 5]),
        (("checks", 0, "details", "guest", "gpu_nodes", 1, "minor"), 0),
        (("checks", 0, "details", "guest", "gpu_nodes", 1, "character"), 1),
        (("checks", 1, "details", "verdict"), "time_limit"),
        (("checks", 3, "details", "observation", "id"), "another"),
        (("checks", 3, "details", "observation", "status"), "stopped"),
        (("checks", 4, "details", "observation", "attach_alive"), 1),
        (("checks", 5, "details", "elapsed_seconds"), float("nan")),
        (("checks", 5, "details", "cpu_seconds"), float("inf")),
        (("checks", 5, "details", "cpu_seconds"), 3.0),
        (("checks", 5, "details", "throttled_periods"), 0),
        (("checks", 5, "details", "throttled_periods"), True),
        (("checks", 6, "details", "mib"), 33),
        (("checks", 6, "details", "mib"), True),
        (("checks", 7, "details", "oom_kills"), 0),
        (("checks", 7, "details", "oom_kills"), True),
        (("checks", 8, "details", "children"), 255),
        (("checks", 8, "details", "remaining_tasks"), 7),
        (("checks", 8, "details", "initial_tasks"), 5),
        (("checks", 8, "details", "peak_tasks"), 255),
        (("checks", 8, "details", "limit_events"), 0),
        (("cleanup", 0, "id"), "identity-recovery"),
        (("cleanup", 0, "scope"), "host-container-v1"),
        (("cleanup", 0, "removed"), False),
        (("cleanup", 0, "exit_code"), True),
        (("cleanup", 0, "running_before_cleanup"), 1),
    ],
)
def test_claims_without_exact_measurements_identity_and_cleanup_are_rejected(path, value):
    report = resource_report(gpu=True)
    item = report
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    with pytest.raises(SandboxUnavailable):
        protocol.validate_report(report, REQUEST, target=TARGET, selection=SELECTION)


@pytest.mark.parametrize("section", ["checks", "cleanup"])
def test_missing_reordered_or_duplicate_records_fail_closed(section):
    original = resource_report()
    for mutate in (lambda x: x.pop(), lambda x: x.reverse(), lambda x: x.__setitem__(0, x[1])):
        report = copy.deepcopy(original)
        mutate(report[section])
        with pytest.raises(SandboxUnavailable):
            protocol.validate_report(report, REQUEST, target=None, selection=None)


def test_report_cannot_choose_its_own_target_or_selected_physical_minor():
    report = resource_report(gpu=True)
    for selection in ({**SELECTION, "minor": 0}, {**SELECTION, "driver": "580.95.05"}):
        with pytest.raises(SandboxUnavailable):
            protocol.validate_report(report, REQUEST, target=TARGET, selection=selection)


def test_cpu_probe_never_initializes_cuda(monkeypatch, tmp_path):
    from judge import container_isolation_guest as guest

    monkeypatch.setattr(guest, "cuda_identity", lambda: pytest.fail("CPU probe called CUDA"))
    # Identity file/network actions remain read/write attempts within this synthetic fixture.
    monkeypatch.setattr(
        guest.socket, "create_connection", lambda *_a, **_kw: (_ for _ in ()).throw(OSError())
    )
    original = guest.Path

    def path(name):
        if name == "/praximesh-readonly-check":
            return tmp_path / "missing" / "readonly-check"
        if name == "/dev":
            return tmp_path
        return original(name)

    monkeypatch.setattr(guest, "Path", path)
    value = guest.identity(
        {
            "gpu": False,
            "port": 1,
            "canary": REQUEST["canary"],
            "protected_path": str(tmp_path / "private"),
        }
    )
    assert value["cuda"] is None and value["gpu_nodes"] == [] and value["root_write"] is False


@pytest.mark.parametrize("count,code", [(1, 0), (0, 0), (2, 0), (1, 100)])
def test_mocked_cuda_identity_requires_one_device_and_successful_driver_calls(
    monkeypatch, count, code
):
    import ctypes as c
    import uuid

    from judge import container_isolation_guest as guest

    calls = []
    identifier = uuid.UUID("12345678-1234-5678-9abc-123456789abc")

    class Function:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            calls.append(self.name)
            if self.name == "cuInit":
                return code
            if self.name in {"cuDeviceGet", "cuDeviceGetCount"}:
                c.cast(args[0], c.POINTER(c.c_int))[0] = count if self.name.endswith("Count") else 0
            elif self.name == "cuDeviceGetUuid_v2":
                c.memmove(args[0], identifier.bytes, 16)
            elif self.name == "cuDeviceGetName":
                c.memmove(args[0], b"Synthetic GPU\0", 14)
            elif self.name == "cuDeviceComputeCapability":
                c.cast(args[0], c.POINTER(c.c_int))[0] = 8
                c.cast(args[1], c.POINTER(c.c_int))[0] = 6
            return 0

    class Library:
        def __getattr__(self, name):
            return Function(name)

    monkeypatch.setattr(c, "CDLL", lambda _name: Library())
    if count != 1 or code:
        with pytest.raises(RuntimeError):
            guest.cuda_identity()
        assert "cuDeviceGetUuid_v2" not in calls
    else:
        assert guest.cuda_identity() == {
            "uuid": "GPU-" + str(identifier),
            "model": "Synthetic GPU",
            "architecture": [8, 6],
            "count": 1,
        }
    assert not any("Ctx" in name or "Launch" in name for name in calls)
