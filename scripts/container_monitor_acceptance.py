"""Optional actual NVML checks in the existing container-controller acceptance.

Only metadata queries are issued. The fixed task still has no GPU devices and
runs the existing CPU guest. An occupied device is observed, never interrupted.
"""

import copy
import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET

from judge.container_journal import ContainerJournal
from judge.container_monitor import devices, validate_observation, validate_observer
from judge.gpu_device_lock import DeviceLock
from judge.gpu_monitor import identify, processes, query
from judge.nvml_artifacts import read_manifest
from scripts.container_journal_acceptance import save
from scripts.container_supervision_acceptance import gone


def observation_checks(
    engine, image, task_image, device, out, storage, controls, states, report, passed
):
    from scripts.container_controller_acceptance import (
        Control,
        same_identity,
        state,
        task_tree,
        wait_report,
    )

    target = identify(device)
    xml = query("-i", device, "--query", "--xml-format")
    processes(xml, target)
    nodes = ET.fromstring(xml).findall("gpu/minor_number")
    minor = nodes[0].text if len(nodes) == 1 else ""
    if type(minor) is not str or not minor.isascii() or not minor.isdecimal():
        raise ValueError("Expected one NVIDIA device minor number")
    library = read_manifest(out / "controller" / "context" / "observer")
    observer = {"schema": 1, "target": target, "library": library, "minor": int(minor)}
    validate_observer(observer)
    mapped = devices(observer)
    for name, numbers in mapped.items():
        info = os.lstat(name)
        assert (
            stat.S_ISCHR(info.st_mode)
            and (os.major(info.st_rdev), os.minor(info.st_rdev)) == numbers
        )
    report["observer"] = observer
    path = storage / "nvml-observer"
    config = state(engine, path, task_image, host_processes=True, device_observer=observer)
    states.append((path, config))
    original = json.loads((path / "mounts.json").read_text())

    for name in (
        "missing-devices",
        "missing-card",
        "wrong-minor",
        "wrong-library",
        "wrong-driver",
        "wrong-device",
        "missing-proc",
        "nonempty-caps",
    ):
        value = copy.deepcopy(original)
        if name == "wrong-minor":
            value["device_observer"]["minor"] = (observer["minor"] + 1) % 255
        elif name == "wrong-library":
            value["device_observer"]["library"]["library"]["sha256"] = "0" * 64
        elif name == "wrong-driver":
            value["device_observer"]["target"]["driver"] = "999.99.99"
        elif name == "wrong-device":
            value["device_observer"]["target"]["uuid"] = "GPU-" + str(uuid.uuid4())
        save(path / "mounts.json", value)
        selection = (
            ()
            if name == "missing-devices"
            else ("/dev/nvidiactl",)
            if name == "missing-card"
            else tuple(mapped)
        )
        control = Control(
            engine,
            image,
            path,
            config,
            "observe-device-nonempty-caps" if name == "nonempty-caps" else "observe-device",
            host_processes=name != "missing-proc",
            observation_devices=selection,
        )
        controls.append(control)
        control.start()
        error = control.wait(1).strip()
        assert error in {"SandboxUnavailable", "FileNotFoundError"}, error
        assert not (path / "active.json").exists()
        with DeviceLock(path / "locks", device) as guard:
            with ContainerJournal(path / "journal", guard, engine) as journal:
                assert journal.load() is None and journal.identity == config["installation"]
        report["cleanup"].append(control.cleanup())
        passed(
            "nvml-" + name,
            {"controller": control.container_id, "task_created": False, "failure_class": error},
        )
    save(path / "mounts.json", original)

    host_before = processes(query("-i", device, "--query", "--xml-format"), target)
    control = Control(
        engine,
        image,
        path,
        config,
        "observe-device",
        host_processes=True,
        observation_devices=tuple(mapped),
    )
    controls.append(control)
    control.start()
    active = wait_report(control, path, "active")
    same_identity(active, config)
    task_processes = task_tree(engine, active)
    host_after = processes(query("-i", device, "--query", "--xml-format"), target)
    initial = validate_observation(active["observation"], observer)
    during = validate_observation(active["during_task"], observer)
    assert sorted(set(host_before)) == initial == during == sorted(set(host_after)), (
        "Inventory changed during comparison; retain the original observation"
    )
    assert active["private_pid"] == 1 and active["host_pid"] != 1
    assert active["external_occupation_rejected"] == bool(initial)
    assert active["foreign_task_processes_rejected"] == bool(during)
    report["real_external_gpu_processes_observed"] = bool(initial)
    (path / "continue").touch(mode=0o600)
    finished = wait_report(control, path, "finished")
    control.wait()
    gone(task_processes)
    assert finished["completion"]["ready"] is False and finished["cleaned_monitor_rejected"] is True
    report["cleanup"].append({"container": active["container"], "removed": True, "kind": "task"})
    passed(
        "nvml-host-pids-and-original-task",
        {
            "controller": control.container_id,
            "host_before": host_before,
            "host_after": host_after,
            "active": active,
            "finished": finished,
            "original_processes": task_processes,
            "process_tree_gone": True,
        },
    )

    # Retain the old explicit procfs contract with no management library/device requirement.
    path = storage / "v2-observer-compatibility"
    config = state(engine, path, task_image, host_processes=True)
    states.append((path, config))
    control = Control(engine, image, path, config, "normal", host_processes=True)
    controls.append(control)
    control.start()
    active = wait_report(control, path, "active")
    same_identity(active, config)
    task_processes = task_tree(engine, active)
    (path / "continue").touch(mode=0o600)
    finished = wait_report(control, path, "finished")
    control.wait()
    gone(task_processes)
    assert finished["completion"]["ready"] is False
    report["cleanup"].append({"container": active["container"], "removed": True, "kind": "task"})
    passed(
        "v2-observer-compatibility",
        {
            "controller": control.container_id,
            "active": active,
            "finished": finished,
            "original_processes": task_processes,
            "process_tree_gone": True,
        },
    )
