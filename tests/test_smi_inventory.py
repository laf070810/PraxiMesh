"""Full inventory validation; fixtures do not prove physical GPU process coverage."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from judge import container_monitor as monitor_module
from judge import container_processes as process_module
from judge.container_monitor import ContainerDeviceMonitor, validate_observation, validate_observer
from judge.container_processes import ProcessDisappeared
from judge.nvml_artifacts import (
    DIRECTORY,
    INVENTORY_PROFILE,
    manifest_digest,
    validate_manifest,
    verify_bundle,
)
from judge.sandbox import SandboxUnavailable
from judge.smi_inventory import MAX_PIDS, MAX_XML, inventory, utility_version
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_monitor import bundle, monitored, observation, saved  # noqa: F401
from tests.test_container_processes import observed_task  # noqa: F401


def upgrade(value):
    value["schema"] = 2
    value["library"].update(
        schema=2,
        profile=INVENTORY_PROFILE,
        utility={"name": "nvidia-smi", "size": 5, "sha256": hashlib.sha256(b"query").hexdigest()},
    )
    return value


@pytest.fixture
def full(saved, bundle):  # noqa: F811
    upgrade(saved)
    path, manifest = bundle
    utility = path / "nvidia-smi"
    utility.write_bytes(b"query")
    utility.chmod(0o755)
    (path / manifest["library"]["soname"]).symlink_to(manifest["library"]["name"])
    (path / "manifest.json").write_text(json.dumps(manifest))
    return saved


def xml(value, rows=""):
    target = value["target"]
    return (
        '<?xml version="1.0" ?>\n'
        '<!DOCTYPE nvidia_smi_log SYSTEM "nvsmi_device_v13.dtd">\n'
        f"<nvidia_smi_log><driver_version>{target['driver']}</driver_version>"
        f"<gpu><uuid>{target['uuid']}</uuid><product_name>{target['model']}</product_name>"
        f"<minor_number>{value['minor']}</minor_number><processes>{rows}</processes>"
        "</gpu></nvidia_smi_log>"
    ).encode()


def row(pid, kind="O"):
    return (
        f"<process_info><pid>{pid}</pid><type>{kind}</type>"
        "<process_name>private name</process_name><used_memory>N/A</used_memory></process_info>"
    )


def version(value="590.48.01"):
    return (
        f"NVIDIA-SMI version : {value}\nNVML version : 590.48\n"
        f"DRIVER version : {value}\nCUDA Version : 13.1\n"
    ).encode()


def complete(value, pids=()):
    result = observation(value)
    result.update(schema=2, smi_version=value["target"]["driver"])
    result["pids"]["inventory"] = list(pids)
    return result


def test_old_context_profile_is_not_a_full_inventory(full, bundle):  # noqa: F811
    assert verify_bundle(full["library"], bundle[0]) == manifest_digest(full["library"])
    validate_observer(full)
    old = copy.deepcopy(full)
    old["schema"] = 1
    with pytest.raises(SandboxUnavailable):
        validate_observer(old)
    with pytest.raises(SandboxUnavailable):
        validate_observation(observation(full), full)


@pytest.mark.parametrize("defect", ["hash", "path", "size", "schema", "profile", "missing"])
def test_utility_manifest_cannot_redirect_or_downgrade(full, defect):
    value = full["library"]
    if defect == "hash":
        value["utility"]["sha256"] = "not a digest"
    elif defect == "path":
        value["utility"]["name"] = "/usr/bin/nvidia-smi"
    elif defect == "size":
        value["utility"]["size"] = True
    elif defect == "schema":
        value["schema"] = 1
    elif defect == "profile":
        value["profile"] = "nvml-observer-v1"
    else:
        value.pop("utility")
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize(
    "defect", ["bytes", "utility-link", "alias", "alias-file", "mode", "extra"]
)
def test_utility_and_exact_library_alias_are_rechecked(full, bundle, defect):  # noqa: F811
    path = bundle[0]
    if defect == "bytes":
        (path / "nvidia-smi").write_bytes(b"other")
    elif defect == "utility-link":
        (path / "nvidia-smi").unlink()
        (path / "nvidia-smi").symlink_to("/usr/bin/nvidia-smi")
    elif defect in {"alias", "alias-file"}:
        (path / "libnvidia-ml.so.1").unlink()
        if defect == "alias":
            (path / "libnvidia-ml.so.1").symlink_to("/usr/lib64/libnvidia-ml.so.1")
        else:
            (path / "libnvidia-ml.so.1").write_bytes(b"fixture")
    elif defect == "mode":
        (path / "nvidia-smi").chmod(0o777)
    else:
        (path / "libcuda.so.1").touch()
    with pytest.raises((ValueError, OSError)):
        verify_bundle(full["library"], path)


def test_all_resource_types_and_unknown_names_are_occupation(full):
    rows = "".join(
        row(100 + i, kind) for i, kind in enumerate(("C", "G", "M", "O", "M+C", "future"))
    )
    assert inventory(xml(full, rows), full) == list(range(100, 106))
    assert inventory(xml(full), full) == []
    assert "private name" not in str(inventory(xml(full, row(22)), full))


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "unavailable",
        "duplicate-list",
        "duplicate-pid",
        "duplicate-field",
        "duplicate-uuid",
        "foreign-device",
        "wrong-driver",
        "wrong-minor",
        "second-gpu",
        "pid-zero",
        "pid-limit",
        "pid-unicode",
        "mixed-text",
        "bad-node",
        "entity",
        "oversized",
        "too-many",
        "broken",
    ],
)
def test_incomplete_or_ambiguous_full_query_never_means_idle(full, defect):
    data = xml(full, row(123))
    replacements = {
        "missing": (b"processes", b"missing"),
        "unavailable": (row(123).encode(), b"N/A"),
        "duplicate-list": (b"</processes>", b"</processes><processes/>"),
        "duplicate-pid": (row(123).encode(), (row(123) + row(123)).encode()),
        "duplicate-field": (b"<pid>123</pid>", b"<pid>123</pid><pid>456</pid>"),
        "duplicate-uuid": (b"</uuid>", b"</uuid><uuid>ambiguous</uuid>"),
        "foreign-device": (full["target"]["uuid"].encode(), b"GPU-foreign"),
        "wrong-driver": (b"590.48.01", b"999.99.99"),
        "wrong-minor": (b"<minor_number>0</minor_number>", b"<minor_number>1</minor_number>"),
        "second-gpu": (b"</gpu>", b"</gpu><gpu/>"),
        "pid-zero": (b"<pid>123</pid>", b"<pid>0</pid>"),
        "pid-limit": (b"<pid>123</pid>", b"<pid>2147483648</pid>"),
        "pid-unicode": (b"<pid>123</pid>", "<pid>１２３</pid>".encode()),
        "mixed-text": (b"</process_info>", b"</process_info>N/A"),
        "bad-node": (b"process_info", b"unknown"),
        "entity": (b"<nvidia_smi_log>", b'<!ENTITY data "bad"><nvidia_smi_log>'),
        "broken": (b"</processes>", b""),
    }
    if defect == "oversized":
        data = b" " * (MAX_XML + 1)
    elif defect == "too-many":
        data = xml(full, "".join(row(i + 1) for i in range(MAX_PIDS + 1)))
    else:
        data = data.replace(*replacements[defect])
    with pytest.raises(SandboxUnavailable):
        inventory(data, full)


@pytest.mark.parametrize("defect", ["old", "missing", "duplicate", "unknown", "non-ascii", "large"])
def test_query_utility_version_is_not_inferred_from_the_library(full, defect):
    data = version()
    assert utility_version(data, full["target"]["driver"]) == "590.48.01"
    if defect == "old":
        data = data.replace(b"NVIDIA-SMI version : 590.48.01", b"NVIDIA-SMI version : 580.1.0")
    elif defect == "missing":
        data = data.split(b"CUDA Version")[0]
    elif defect == "duplicate":
        data += b"DRIVER version : 590.48.01\n"
    elif defect == "unknown":
        data += b"EXTRA : 1\n"
    elif defect == "non-ascii":
        data += b"\xff"
    else:
        data += b" " * 4096
    with pytest.raises(SandboxUnavailable):
        utility_version(data, full["target"]["driver"])


def test_management_only_image_does_not_require_a_cuda_version():
    assert utility_version(version().replace(b"13.1", b"N/A"), "590.48.01") == "590.48.01"
    for field in (b"NVIDIA-SMI version", b"NVML version", b"DRIVER version"):
        data = b"\n".join(
            line.split(b":")[0] + b": N/A" if line.startswith(field) else line
            for line in version().splitlines()
        )
        with pytest.raises(SandboxUnavailable):
            utility_version(data, "590.48.01")


def test_native_contexts_must_be_in_full_inventory(full):
    value = complete(full, [200, 300])
    value["pids"]["compute"] = [200]
    assert validate_observation(value, full) == [200, 300]
    value["pids"]["graphics"] = [400]
    with pytest.raises(SandboxUnavailable, match="sources disagree"):
        validate_observation(value, full)


@pytest.mark.parametrize("before,after", [([200], []), ([], [200]), ([200], [200])])
def test_xml_window_keeps_a_process_starting_or_ending_between_queries(full, before, after):
    value = complete(full, [200])
    value.update(schema=3, inventory_samples=[before, after])
    value["pids"]["compute"] = [200]
    assert validate_observation(value, full) == [200]
    value["inventory_samples"] = [[], []]
    with pytest.raises(SandboxUnavailable, match="samples changed"):
        validate_observation(value, full)
    value["pids"]["inventory"] = []
    with pytest.raises(SandboxUnavailable, match="sources disagree"):
        validate_observation(value, full)


@pytest.mark.parametrize(
    "samples", [None, [], [[200]], [[], [], []], [[True], []], [[200, 200], []], [[200], [0]]]
)
def test_complete_window_requires_two_valid_original_samples(full, samples):
    value = complete(full, [200])
    value.update(schema=3, inventory_samples=samples)
    with pytest.raises(SandboxUnavailable):
        validate_observation(value, full)


@pytest.fixture
def combined(monitored, monkeypatch):  # noqa: F811
    item = monitored
    selected = upgrade(copy.deepcopy(item.monitor.saved))
    item.task.control.saved = {"version": 4, "device_observer": selected}
    item.monitor = ContainerDeviceMonitor(item.task.control)
    item.value = complete(selected)

    def read(*, on_sample=None):
        value = copy.deepcopy(item.value)
        if on_sample is not None:
            for sample in value.get("inventory_samples", [value["pids"]["inventory"]]):
                on_sample(sample)
        return value

    class Handle:
        # Source-order fixtures use synthetic PIDs. Actual Linux handle and
        # reaping checks live in test_container_process_samples.py.
        def __init__(self, view, pid, group):
            self.view, self.pid = view, pid
            self.identity = view.read_observed(pid)
            if self.identity["cgroup"] != group:
                raise SandboxUnavailable("An observed host process is outside the original task")

        def check(self, *, final=False):
            try:
                current = self.view.read_observed(self.pid)
            except ProcessDisappeared:
                if final and self.exited():
                    return
                raise
            if current != self.identity:
                raise SandboxUnavailable("Sampled identity changed")

        def close(self):
            pass

        def exited(self):
            return (
                self.pid in getattr(item.task, "exited_pids", set())
                or not item.task.state["State"]["Running"]
            )

    monkeypatch.setattr(process_module, "SampledProcess", Handle)
    monkeypatch.setattr(item.monitor, "read", read)
    return item


def test_other_only_pid_is_rejected_without_signalling(combined):
    item = combined
    item.value["pids"]["inventory"] = [9999]
    with pytest.raises(SandboxUnavailable, match="occupied"):
        item.monitor.observe()
    with pytest.raises(SandboxUnavailable, match="outside"):
        item.monitor.observe(item.task.service)
    assert not any(call[:2] == ("container", "kill") for call in item.task.journal.engine.calls)


def test_later_empty_xml_never_hides_an_earlier_external_process(combined):
    item = combined
    item.value = complete(item.monitor.saved, [9999])
    item.value.update(schema=3, inventory_samples=[[9999], []])
    with pytest.raises(SandboxUnavailable, match="occupied"):
        item.monitor.observe()
    with pytest.raises(SandboxUnavailable, match="outside"):
        item.monitor.observe(item.task.service)


@pytest.mark.parametrize("ending", [False, True])
def test_live_query_preserves_both_xml_samples_around_native_contexts(
    combined, monkeypatch, ending
):
    item = combined
    monkeypatch.delattr(item.monitor, "read")
    queries = []

    def run(command, **options):
        queries.append((command, options))
        options["on_tick"]()
        if len(queries) == 1:
            data = version()
        elif len(queries) == 3:
            value = observation(item.monitor.saved)
            value["pids"]["compute"] = [5678]
            data = json.dumps(value).encode()
        else:
            positive = (len(queries) == 2) == ending
            data = xml(item.monitor.saved, row(5678, "C") if positive else "")
        return SimpleNamespace(stdout=data, stderr=b"")

    monkeypatch.setattr(monitor_module, "run_command", run)
    captured = []
    sample_order = []
    original = item.monitor.read

    def read(*, on_sample=None):
        def capture(pids):
            sample_order.append((len(queries), list(pids)))
            on_sample(pids)

        value = original(on_sample=capture)
        captured.append(value)
        return value

    monkeypatch.setattr(item.monitor, "read", read)
    assert item.monitor.observe(item.task.service) == [5678]
    assert captured[0]["schema"] == 3
    assert captured[0]["inventory_samples"] == ([[5678], []] if ending else [[], [5678]])
    assert sample_order == ([(2, [5678]), (4, [])] if ending else [(2, []), (4, [5678])])
    assert len(queries) == 4 and len({v[1]["deadline"] for v in queries}) == 1


def test_empty_inventory_preserves_original_anchor_and_cleanup_boundary(combined):
    item = combined
    assert item.monitor.observe(item.task.service) == []
    item.task.journal.recover()
    with pytest.raises(SandboxUnavailable, match="record changed"):
        item.monitor.observe(item.task.service)


def test_task_selection_uses_pinned_device_and_kernel_registry(combined, monkeypatch):
    item = combined
    item.task.control.host_processes.fd = 99
    calls = []

    def read(fd, name, maximum):
        calls.append((fd, name, maximum))
        return "Character devices:\n195 nvidia\n507 nvidia-uvm\nBlock devices:\n"

    monkeypatch.setattr(monitor_module, "read_file", read)
    value = item.monitor.task_device_spec()
    assert value["uuid"] == item.task.service.guard.device_uuid
    assert value["minor"] == item.monitor.saved["minor"] and value["uvm_major"] == 507
    assert calls == [(99, "devices", 65536)]


def test_legacy_context_observer_cannot_prepare_device_task(monitored):  # noqa: F811
    with pytest.raises(SandboxUnavailable, match="complete process source"):
        monitored.monitor.task_device_spec()


@pytest.mark.parametrize(
    "defect",
    [None, "version", "xml", "late-xml", "warning", "partial", "deadline", "native-schema"],
)
def test_one_fixed_deadline_covers_all_sources(combined, monkeypatch, defect):
    item = combined
    monkeypatch.delattr(item.monitor, "read")
    calls = []

    def run(command, **options):
        calls.append((command, options))
        options["on_tick"]()
        if len(calls) == 3:
            value = observation(item.monitor.saved)
            if defect == "native-schema":
                value["schema"] = 2
            if defect == "partial":
                value["pids"]["compute"] = [5678]
            return SimpleNamespace(stdout=json.dumps(value).encode(), stderr=b"")
        if defect == "deadline" and len(calls) == 4:
            raise SandboxUnavailable("Private runtime command exceeded its deadline")
        data = version("580.1.0" if defect == "version" else "590.48.01")
        if len(calls) in {2, 4}:
            invalid = (
                defect == "xml" and len(calls) == 2 or defect == "late-xml" and len(calls) == 4
            )
            data = b"N/A" if invalid else xml(item.monitor.saved)
        return SimpleNamespace(
            stdout=data, stderr=b"warning" if defect == "warning" and len(calls) == 4 else b""
        )

    monkeypatch.setattr(monitor_module, "run_command", run)
    if defect is None:
        assert item.monitor.observe() == []
        assert len(calls) == 4
        assert len({options["deadline"] for _, options in calls}) == 1
        for command, options in (calls[0], calls[1], calls[3]):
            assert command[0] == str(DIRECTORY / "nvidia-smi")
            assert options["env"] == {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "LD_LIBRARY_PATH": str(DIRECTORY),
            }
        assert calls[0][0][1:] == ["--version"]
        assert (
            calls[1][0][1:]
            == calls[3][0][1:]
            == [
                "-i",
                item.monitor.saved["target"]["uuid"],
                "--query",
                "--xml-format",
            ]
        )
        assert calls[1][1]["max_output"] == calls[3][1]["max_output"] == MAX_XML
    else:
        with pytest.raises(SandboxUnavailable):
            item.monitor.observe()
