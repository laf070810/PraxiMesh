"""NVML provenance and original-task refusal checks; no hardware emulation claim."""

import copy
import ctypes
import hashlib
import json
from types import SimpleNamespace

import pytest

from judge import container_monitor as module
from judge.container_monitor import (
    ContainerDeviceMonitor,
    check_empty_caps,
    devices,
    validate_observation,
    validate_observer,
)
from judge.nvml_artifacts import PROFILE, manifest_digest, validate_manifest, verify_bundle
from judge.nvml_reader import MAX_PIDS, NVMLFailure, ProcessInfo, process_list
from judge.sandbox import SandboxUnavailable
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_processes import observed_task  # noqa: F401


@pytest.fixture
def bundle(tmp_path):
    value = {
        "schema": 1,
        "profile": PROFILE,
        "driver_version": "590.48.01",
        "library": {
            "name": "libnvidia-ml.so.590.48.01",
            "soname": "libnvidia-ml.so.1",
            "size": 7,
            "sha256": hashlib.sha256(b"fixture").hexdigest(),
        },
    }
    (tmp_path / value["library"]["name"]).write_bytes(b"fixture")
    (tmp_path / "manifest.json").write_text(json.dumps(value))
    return tmp_path, value


@pytest.fixture
def saved(bundle):
    return {
        "schema": 1,
        "target": {
            "index": 2,  # Host enumeration is not the character-device minor.
            "uuid": "GPU-11111111-2222-3333-4444-555555555555",
            "model": "NVIDIA fixture",
            "driver": "590.48.01",
            "architecture": [8, 6],
        },
        "library": bundle[1],
        "minor": 0,
    }


def observation(saved, *, pids=None):
    return {
        "schema": 1,
        "bundle": manifest_digest(saved["library"]),
        **{
            key: copy.deepcopy(saved["target"][key])
            for key in ("uuid", "model", "driver", "architecture")
        },
        "minor": saved["minor"],
        "nvml_version": "13.590.48.01",
        "pids": pids or {"compute": [], "graphics": [], "mps": []},
    }


@pytest.mark.parametrize(
    "defect", ["schema", "profile", "name", "version", "size", "hash", "soname", "extra"]
)
def test_management_manifest_cannot_select_another_library_or_profile(bundle, defect):
    _, value = bundle
    if defect == "schema":
        value["schema"] = True
    elif defect == "profile":
        value["profile"] = "cuda-ptx-driver-v1"
    elif defect == "name":
        value["library"]["name"] = "../../libcuda.so.1"
    elif defect == "version":
        value["driver_version"] = "../version"
    elif defect == "size":
        value["library"]["size"] = True
    elif defect == "hash":
        value["library"]["sha256"] = "unknown"
    elif defect == "soname":
        value["library"]["soname"] = "libcuda.so.1"
    else:
        value["ready"] = True
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize("defect", ["bytes", "symlink", "manifest", "extra"])
def test_changed_or_expanded_bundle_is_rejected_before_loading(bundle, defect):
    path, value = bundle
    assert verify_bundle(value, path) == manifest_digest(value)
    library = path / value["library"]["name"]
    if defect == "bytes":
        library.write_bytes(b"changed")
    elif defect == "symlink":
        other = path / "other"
        library.rename(other)
        library.symlink_to(other)
    elif defect == "manifest":
        altered = copy.deepcopy(value)
        altered["library"]["sha256"] = "0" * 64
        (path / "manifest.json").write_text(json.dumps(altered))
    else:
        (path / "libcuda.so.1").write_bytes(b"not permitted")
    with pytest.raises((OSError, ValueError)):
        verify_bundle(value, path)


def test_minor_mapping_is_independent_of_host_enumeration(saved):
    assert devices(saved) == {"/dev/nvidiactl": (195, 255), "/dev/nvidia0": (195, 0)}


@pytest.mark.parametrize("defect", [None, "file", "symlink-child", "directory-symlink", "writable"])
def test_nvml_created_capability_directory_never_admits_extra_nodes(tmp_path, defect):
    path = tmp_path / "nvidia-caps"
    path.mkdir(mode=0o755)
    if defect == "file":
        (path / "nvidia-cap1").touch()
    elif defect == "symlink-child":
        (path / "nvidia-cap1").symlink_to("/dev/null")
    elif defect == "directory-symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other, target_is_directory=True)
    elif defect == "writable":
        path.chmod(0o777)
    if defect is None:
        check_empty_caps(path)
    else:
        with pytest.raises((OSError, SandboxUnavailable)):
            check_empty_caps(path)


@pytest.mark.parametrize(
    "defect",
    ["schema", "minor-bool", "minor-reserved", "driver", "uuid", "target-extra", "architecture"],
)
def test_unknown_device_manifest_is_not_a_query_capability(saved, defect):
    if defect == "schema":
        saved["schema"] = True
    elif defect == "minor-bool":
        saved["minor"] = False
    elif defect == "minor-reserved":
        saved["minor"] = 255
    elif defect == "driver":
        saved["target"]["driver"] = "999.99.99"
    elif defect == "uuid":
        saved["target"]["uuid"] = "0"
    elif defect == "target-extra":
        saved["target"]["command"] = "anything"
    else:
        saved["target"]["architecture"][0] = True
    with pytest.raises(SandboxUnavailable):
        validate_observer(saved)


def test_every_process_category_counts_and_cross_category_duplicates_are_unioned(saved):
    value = observation(saved, pids={"compute": [100, 200], "graphics": [100, 300], "mps": [400]})
    assert validate_observation(value, saved) == [100, 200, 300, 400]


@pytest.mark.parametrize(
    "defect",
    [
        "schema",
        "bundle",
        "driver",
        "uuid",
        "minor",
        "missing-kind",
        "extra-kind",
        "unknown-list",
        "pid-bool",
        "duplicate",
        "oversized",
        "nvml-version",
    ],
)
def test_partial_or_malformed_query_is_never_empty_or_owned(saved, defect):
    value = observation(saved)
    if defect == "schema":
        value["schema"] = True
    elif defect == "bundle":
        value["bundle"] = "0" * 64
    elif defect == "driver":
        value["driver"] = "999.99.99"
    elif defect == "uuid":
        value["uuid"] = "wrong"
    elif defect == "minor":
        value["minor"] = False
    elif defect == "missing-kind":
        value["pids"].pop("graphics")
    elif defect == "extra-kind":
        value["pids"]["other"] = []
    elif defect == "unknown-list":
        value["pids"]["mps"] = None
    elif defect == "pid-bool":
        value["pids"]["compute"] = [True]
    elif defect == "duplicate":
        value["pids"]["compute"] = [123, 123]
    elif defect == "oversized":
        value["pids"]["compute"] = list(range(1, MAX_PIDS + 1))
        value["pids"]["graphics"] = [MAX_PIDS + 1]
    else:
        value["nvml_version"] = "N/A"
    with pytest.raises(SandboxUnavailable):
        validate_observation(value, saved)


@pytest.mark.parametrize("code", [2, 3, 4, 7, 9, 15, 999])
def test_native_errors_including_unsupported_and_insufficient_size_are_not_empty(code):
    def failed(*_args):
        return code

    with pytest.raises(NVMLFailure, match=f"failed: {code}"):
        process_list(failed, ctypes.c_void_p(1))


@pytest.mark.parametrize("defect", ["none", "duplicate", "mig", "pid", "count"])
def test_native_v3_array_is_bounded_and_whole_device(defect):
    assert ctypes.sizeof(ProcessInfo) == 24

    def native(_handle, count, rows):
        assert count._obj.value == MAX_PIDS
        count._obj.value = MAX_PIDS + 1 if defect == "count" else 2
        for i in range(2):
            rows[i].pid = 1234 if defect == "duplicate" else 1234 + i
            rows[i].gpu_instance = rows[i].compute_instance = 0xFFFFFFFF
        if defect == "mig":
            rows[0].gpu_instance = 1
        elif defect == "pid":
            rows[0].pid = 0
        return 0

    if defect == "none":
        assert process_list(native, ctypes.c_void_p(1)) == [1234, 1235]
    else:
        with pytest.raises(NVMLFailure):
            process_list(native, ctypes.c_void_p(1))


@pytest.fixture
def monitored(observed_task, saved, monkeypatch):  # noqa: F811
    task = observed_task
    saved["target"]["uuid"] = task.service.guard.device_uuid
    task.control.guard = task.service.guard
    task.control.saved = {"version": 3, "device_observer": saved}
    monitor = ContainerDeviceMonitor(task.control)
    value = observation(saved)
    monkeypatch.setattr(monitor, "read", lambda: copy.deepcopy(value))
    return SimpleNamespace(task=task, monitor=monitor, value=value)


def test_idle_monitor_rejects_any_external_gpu_category_without_signalling(monitored):
    item = monitored
    assert item.monitor.observe() == []
    item.value["pids"]["graphics"] = [9999]
    with pytest.raises(SandboxUnavailable, match="occupied"):
        item.monitor.observe()
    assert not any(call[:2] == ("container", "kill") for call in item.task.journal.engine.calls)


def test_running_monitor_preserves_one_original_anchor_even_on_empty_inventory(monitored):
    item = monitored
    item.value["pids"]["compute"] = [5678]
    assert item.monitor.observe(item.task.service) == [5678]
    anchored = item.monitor.owned
    item.value["pids"]["compute"] = []
    assert item.monitor.observe(item.task.service) == []
    assert item.monitor.owned is anchored
    item.task.members[1234]["birth"] += 1
    with pytest.raises(SandboxUnavailable, match="changed"):
        item.monitor.observe(item.task.service)


def test_foreign_process_is_rejected_and_cleaned_record_cannot_be_rebound(monitored):
    item = monitored
    item.value["pids"]["mps"] = [9999]
    with pytest.raises(SandboxUnavailable, match="outside"):
        item.monitor.observe(item.task.service)
    item.value["pids"]["mps"] = []
    item.task.journal.recover()
    with pytest.raises(SandboxUnavailable, match="record changed"):
        item.monitor.observe(item.task.service)
    with pytest.raises(SandboxUnavailable, match="switch"):
        item.monitor.observe()


def test_changed_selected_device_is_not_reinterpreted(monitored):
    item = monitored
    item.task.control.saved["device_observer"]["minor"] = 1
    with pytest.raises(SandboxUnavailable, match="changed its selected device"):
        item.monitor.observe()


def test_query_uses_only_fixed_isolated_command_and_bounded_metadata(monitored, monkeypatch):
    item = monitored
    # Exercise the real read method rather than the inventory-only boundary fixture.
    monkeypatch.delattr(item.monitor, "read")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["on_tick"]()
        return SimpleNamespace(stdout=json.dumps(item.value).encode())

    monkeypatch.setattr(module, "run_command", run)
    assert item.monitor.read() == item.value
    command, options = calls[0]
    assert command[1] == "-I" and command[2].endswith("/judge/nvml_reader.py")
    assert command[3:] == [item.task.service.guard.device_uuid]
    assert options["env"] == {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}
    assert options["seconds"] == 3 and options["max_output"] == 256 * 1024


def test_query_failure_does_not_synthesize_an_idle_inventory(monitored, monkeypatch):
    item = monitored
    monkeypatch.delattr(item.monitor, "read")

    def failed(*_args, **_kwargs):
        raise SandboxUnavailable("Fixed query failed")

    monkeypatch.setattr(module, "run_command", failed)
    with pytest.raises(SandboxUnavailable, match="query failed"):
        item.monitor.observe()


@pytest.mark.parametrize(
    "failed_api", [None, "nvmlDeviceGetMPSComputeRunningProcesses_v3", "nvmlDeviceGetUUID"]
)
def test_native_reader_shuts_down_after_success_or_partial_query_failure(
    saved, monkeypatch, failed_api
):
    from judge import nvml_reader as reader

    calls = []
    fields = {
        "nvmlDeviceGetUUID": saved["target"]["uuid"],
        "nvmlDeviceGetName": saved["target"]["model"],
        "nvmlSystemGetDriverVersion": saved["target"]["driver"],
        "nvmlSystemGetNVMLVersion": "13.590.48.01",
    }

    class Call:
        def __init__(self, name):
            self.__name__ = name

        def __call__(self, *args):
            name = self.__name__
            calls.append(name)
            if name == failed_api:
                return 3
            if name in fields:
                args[-2].value = fields[name].encode()
            elif name == "nvmlDeviceGetHandleByUUID":
                args[-1]._obj.value = 42
            elif name == "nvmlDeviceGetMinorNumber":
                args[-1]._obj.value = saved["minor"]
            elif name == "nvmlDeviceGetCudaComputeCapability":
                args[-2]._obj.value, args[-1]._obj.value = saved["target"]["architecture"]
            elif name.endswith("RunningProcesses_v3"):
                args[-2]._obj.value = 0
            return 0

    class Library:
        def __getattr__(self, name):
            return Call(name)

    monkeypatch.setattr(reader.C, "CDLL", lambda _path: Library())
    monkeypatch.setattr(reader, "read_manifest", lambda: saved["library"])
    monkeypatch.setattr(reader, "verify_bundle", lambda _value: manifest_digest(saved["library"]))
    if failed_api:
        with pytest.raises(NVMLFailure, match=failed_api):
            reader.observe(saved["target"]["uuid"])
    else:
        result = reader.observe(saved["target"]["uuid"])
        assert validate_observation(result, saved) == []
        for kind in ("Compute", "Graphics", "MPSCompute"):
            assert f"nvmlDeviceGet{kind}RunningProcesses_v3" in calls
    assert calls[0] == "nvmlInit_v2" and calls[-1] == "nvmlShutdown"
    assert calls.count("nvmlShutdown") == 1
