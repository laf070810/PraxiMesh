"""Single-card creation and frozen recovery boundaries; no CUDA emulation."""

import copy
import json
import uuid

import pytest

from judge.container_contract import WORKLOAD_PROFILE, docker_resources, resource_marker
from judge.container_devices import (
    PROFILE,
    docker_devices,
    kernel_uvm_major,
    nodes,
    validate_selection,
)
from judge.container_gpu import GPUContainerService, image_contract
from judge.container_gpu_runtime import GPUContainerRuntime
from judge.container_journal import PersistentRuntimeService, RecoveryContainer
from judge.container_oci import ContainerRuntime
from judge.container_service import PREFIX, digest
from judge.sandbox import SandboxUnavailable
from tests.test_container_driver import bundle  # noqa: F401
from tests.test_container_journal import journal  # noqa: F401
from tests.test_container_supervision import IMAGE, snapshot


@pytest.fixture
def selected(journal):  # noqa: F811
    return {
        "schema": 1,
        "profile": PROFILE,
        "uuid": journal.guard.device_uuid,
        "driver": "590.48.01",
        "minor": 2,
        "uvm_major": 509,
    }


@pytest.fixture
def task(journal, bundle, selected):  # noqa: F811
    return device_intent(journal, bundle[1], selected)


def device_intent(record_store, manifest, selection, job_id=None, reservation=None, purpose="task"):
    service = GPUContainerService(
        record_store,
        job_id or uuid.uuid4(),
        reservation or uuid.uuid4(),
        image=IMAGE,
        driver_manifest=manifest,
        device_spec=selection,
        purpose=purpose,
    )
    service.image_env = []
    state = snapshot(service)
    state["Config"]["Cmd"][3] = resource_marker(WORKLOAD_PROFILE)
    state["Config"]["Labels"].update(service.required_image_labels())
    state["Config"]["Labels"][PREFIX + "command"] = digest(state["Config"]["Cmd"])
    state["HostConfig"].update(
        docker_resources(WORKLOAD_PROFILE), Devices=docker_devices(selection)
    )
    service.record_intent(state["Config"]["Cmd"])
    state["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges",
        "seccomp=" + json.dumps(record_store.load()["security"][1]),
    ]
    record_store.engine.state = state
    return service, state


@pytest.mark.parametrize("defect", ["schema", "profile", "uuid", "minor", "uvm", "path", "driver"])
def test_selection_cannot_request_another_profile_or_arbitrary_device(selected, defect):
    if defect == "schema":
        selected["schema"] = True
    elif defect == "profile":
        selected["profile"] = "all-devices"
    elif defect == "uuid":
        selected["uuid"] = "all"
    elif defect == "minor":
        selected["minor"] = 255
    elif defect == "uvm":
        selected["uvm_major"] = 195
    elif defect == "path":
        selected["path"] = "/dev/mem"
    else:
        selected["driver"] = "590.48"
    with pytest.raises(ValueError):
        validate_selection(selected)


@pytest.mark.parametrize(
    "registry",
    [
        "",
        "Character devices:\n\nBlock devices:\n509 nvidia-uvm\n",
        "Character devices:\n509 nvidia-uvm\n508 nvidia-uvm\nBlock devices:\n",
        "Character devices:\nN/A nvidia-uvm\nBlock devices:\n",
    ],
)
def test_missing_or_ambiguous_uvm_registry_is_not_guessed(registry):
    assert (
        kernel_uvm_major("Character devices:\n195 nvidia\n509 nvidia-uvm\nBlock devices:\n") == 509
    )
    with pytest.raises(ValueError):
        kernel_uvm_major(registry)


def test_outer_and_inner_mapping_select_the_same_three_nodes(selected):
    assert nodes(selected) == {
        "/dev/nvidia2": (195, 2),
        "/dev/nvidiactl": (195, 255),
        "/dev/nvidia-uvm": (509, 0),
    }
    runtime = object.__new__(GPUContainerRuntime)
    runtime.device_spec = copy.deepcopy(selected)
    spec = runtime.specification(["/usr/local/bin/python", "-I", "/fixed.py"])
    assert {
        item["path"]: (item["major"], item["minor"]) for item in spec["linux"]["devices"]
    } == nodes(selected)
    assert spec["linux"]["resources"]["devices"][0] == {"allow": False, "access": "rwm"}
    assert all(rule["access"] == "rw" for rule in spec["linux"]["resources"]["devices"][1:])
    assert "NVIDIA_VISIBLE_DEVICES=void" in spec["process"]["env"]
    assert "devices" not in object.__new__(ContainerRuntime).specification(["/fixed.py"])["linux"]
    assert spec["root"]["readonly"] and spec["process"]["noNewPrivileges"]


def test_service_requires_matching_lock_driver_budget(journal, bundle, selected):  # noqa: F811
    for change in ({"uuid": "GPU-" + str(uuid.uuid4())}, {"driver": "580.01.01"}):
        with pytest.raises(SandboxUnavailable, match="mismatch"):
            GPUContainerService(
                journal,
                uuid.uuid4(),
                uuid.uuid4(),
                image=IMAGE,
                driver_manifest=bundle[1],
                device_spec={**selected, **change},
            )
    with pytest.raises(SandboxUnavailable, match="mismatch"):
        GPUContainerService(
            journal,
            uuid.uuid4(),
            uuid.uuid4(),
            image=IMAGE,
            driver_manifest=bundle[1],
            device_spec=selected,
            resource_profile="container-supervision-v1",
        )
    assert journal.load() is None


def test_v3_records_exact_devices_and_recovers_without_executing_old_code(task, selected):
    service, state = task
    record = service.journal.load()
    assert record["version"] == 3 and record["device_spec"] == selected
    assert record["resource_profile"] == WORKLOAD_PROFILE
    assert record["labels"][PREFIX + "runtime"] == image_contract(service.driver_manifest)
    selected["minor"] = 3
    assert service.device_spec["minor"] == 2 and service.journal.load() == record
    recovery = RecoveryContainer(service.journal, record)
    with pytest.raises(SandboxUnavailable, match="cannot execute"):
        recovery.start(["/unexpected"])
    assert recovery.inspect()["id"] == state["Id"]
    assert service.journal.recover()["ready"] is False
    assert service.journal.load() is None
    assert not any(
        call[:2] in {("container", "start"), ("container", "exec")} for call in service.engine.calls
    )


@pytest.mark.parametrize(
    "defect", ["missing", "extra", "other-card", "permissions", "requests", "host-pid"]
)
def test_changed_device_mapping_never_authorizes_stop(task, defect):
    service, state = task
    host = state["HostConfig"]
    if defect == "missing":
        host["Devices"].pop()
    elif defect == "extra":
        host["Devices"].append(
            {
                "PathOnHost": "/dev/nvidia3",
                "PathInContainer": "/dev/nvidia3",
                "CgroupPermissions": "rw",
            }
        )
    elif defect == "other-card":
        host["Devices"][0]["PathOnHost"] = "/dev/nvidia3"
    elif defect == "permissions":
        host["Devices"][0]["CgroupPermissions"] = "rwm"
    elif defect == "requests":
        host["DeviceRequests"] = [{"Count": -1}]
    else:
        host["PidMode"] = "host"
    with pytest.raises(SandboxUnavailable, match="containment"):
        service.journal.recover()
    assert service.journal.load() is not None
    assert not any(call[:2] == ("container", "kill") for call in service.engine.calls)


@pytest.mark.parametrize("defect", ["downgrade", "missing", "device", "budget", "extra"])
def test_saved_device_semantics_cannot_be_reinterpreted(task, defect):
    service, _ = task
    record_store = service.journal
    record = record_store.load()
    if defect == "downgrade":
        record["version"] = 2
    elif defect == "missing":
        record.pop("device_spec")
    elif defect == "device":
        record["device_spec"]["uuid"] = "GPU-" + str(uuid.uuid4())
    elif defect == "budget":
        record["resource_profile"] = "container-supervision-v1"
    else:
        record["device_spec"]["capabilities"] = "all"
    record_store.write(record_store.filename, record)
    with pytest.raises(SandboxUnavailable, match="durable"):
        record_store.recover()
    assert not any(call[:2] == ("container", "kill") for call in service.engine.calls)


def test_legacy_service_still_rejects_device_tasks(task):
    service, state = task
    legacy = PersistentRuntimeService(
        service.journal, service.job_id, service.reservation_token, image=IMAGE
    )
    legacy.image_env = []
    assert not legacy.valid_devices(state["HostConfig"]["Devices"])
    assert legacy.journal_device_spec() is None


def test_creation_retains_seccomp_and_only_explicit_rw_nodes(task):
    service, _ = task
    with service.creation_options() as options:
        assert options[0].startswith("--security-opt=seccomp=")
        assert options[1:] == (
            "--device=/dev/nvidia2:/dev/nvidia2:rw",
            "--device=/dev/nvidiactl:/dev/nvidiactl:rw",
            "--device=/dev/nvidia-uvm:/dev/nvidia-uvm:rw",
        )
