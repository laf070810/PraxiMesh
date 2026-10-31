"""Actual trusted-container single-card metadata probe; no CUDA workload."""

import copy
import json
import os
import time

from judge.container_contract import PYTHON
from judge.container_contract import STATE as TASK_STATE
from judge.container_controller import STATE
from judge.container_devices import encoded_selection
from judge.container_gpu import GPUContainerService
from judge.container_monitor import ContainerDeviceMonitor
from judge.driver_artifacts import validate_manifest
from judge.sandbox import SandboxUnavailable


def task_report(control, service, deadline, name):
    while time.monotonic() < deadline:
        control.check()
        state = service.inspect()
        if state is None or not state["running"]:
            raise SandboxUnavailable("Original node task exited before observation")
        result = control.engine.run(
            "container",
            "exec",
            service.container_id,
            PYTHON,
            "-I",
            "-c",
            "from pathlib import Path; import sys; "
            f"p=Path('{TASK_STATE}/{name}.json'); "
            f"p=p if p.exists() else Path('{TASK_STATE}/gpu-nodes.json'); "
            "sys.stdout.buffer.write(p.read_bytes()[:65537] if p.exists() else b'')",
        )
        if len(result.stdout) > 65536:
            raise SandboxUnavailable("Device node report exceeded its bound")
        if result.stdout:
            return json.loads(result.stdout)
        time.sleep(0.05)
    raise SandboxUnavailable("Device node report exceeded its deadline")


def run(control, mode, image, job, reservation):
    from scripts.container_controller_fixture import save

    monitor = ContainerDeviceMonitor(control)
    if mode == "gpu-nodes-recover":
        recovered = control.journal.recover()
        if recovered is None or recovered["ready"] is not False:
            raise SandboxUnavailable("Expected an original single-device task to recover")
        save("recovered", recovered)
    monitor.observe()
    selection = monitor.task_device_spec()
    driver = validate_manifest(json.loads((STATE / "driver.json").read_text()))
    if mode == "gpu-nodes-wrong-uvm":
        selection = {**selection, "uvm_major": (selection["uvm_major"] + 1) % 4096 or 1}
    service = GPUContainerService(
        control.journal,
        job,
        reservation,
        image=image,
        driver_manifest=driver,
        device_spec=selection,
    )
    report = {
        "gpu_executed": False,
        "cuda_initialized": False,
        "process": control.process_identity,
        "installation": control.journal.identity,
        "owner": service.owner,
        "name": service.name,
        "selection": selection,
    }
    try:
        status = service.start(
            [PYTHON, "-I", "/opt/praximesh/container_gpu_fixture.py", encoded_selection(selection)],
            deadline=time.monotonic() + 80,
            on_tick=control.check,
        )
        report.update(container=service.container_id, status=status, record=control.journal.load())
        if mode == "gpu-nodes-wrong-uvm":
            value = task_report(control, service, status["deadline"] - 4, "gpu-nodes")
            if value["passed"] or not value.get("failure", "").startswith("SandboxUnavailable:"):
                raise SandboxUnavailable("Wrong UVM did not fail before its guest")
            report["expected_rejection"] = value
        else:
            value = task_report(control, service, status["deadline"] - 4, "gpu-nodes-ready")
            if "failure" in value:
                save("task-failure", value)
                raise SandboxUnavailable("Device task failed before guest observation")
            report["guest"] = value
            report["observation"] = monitor.read()
            monitor.observe(service)
            report["task_anchor"] = copy.deepcopy(monitor.owned.check())
            save("active", report)
            if mode == "gpu-nodes-orphan":
                os._exit(17)
            while not (STATE / "continue").exists():
                monitor.observe(service)
                if time.monotonic() >= status["deadline"] - 8:
                    raise TimeoutError("Device node observer did not release its controller")
                time.sleep(0.1)
            control.engine.run(
                "container",
                "exec",
                service.container_id,
                PYTHON,
                "-I",
                "-c",
                f"from pathlib import Path; Path('{TASK_STATE}/observe').touch(exist_ok=False)",
            )
            value = task_report(control, service, status["deadline"] - 4, "gpu-nodes")
            if not value["passed"]:
                raise SandboxUnavailable("Fixed device metadata task did not complete")
            report["result"] = value
    finally:
        report["completion"] = control.journal.recover()
    if monitor.owned is not None:
        try:
            monitor.observe(service)
        except SandboxUnavailable:
            report["cleaned_monitor_rejected"] = True
        else:
            raise SandboxUnavailable("Cleaned GPU task retained its observation anchor")
    idle = ContainerDeviceMonitor(control)
    idle.observe()
    report["ready"] = False
    save("finished", report)
