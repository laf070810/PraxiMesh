"""Container-specific startup evidence; saved environments alone grant no readiness."""

import re
import uuid
from copy import deepcopy

from judge.container_isolation_protocol import resource_request
from judge.container_isolation_protocol import validate_report as resource_report
from judge.container_kernel_checks import validate_check_report
from judge.container_lifecycle import validate_cleanup, validate_report
from judge.controller_faults import fault_request, require
from judge.gpu_kernel_checks import check_request

SCOPE = "container-startup-stage-v1"


def private_identity(guard, adapter):
    """Bind the original controller mounts and real host, not container UID zero."""
    from judge.container_controller import ContainerController

    control = adapter.control
    require(
        type(control) is ContainerController
        and control.guard is guard
        and control.journal.guard is guard
        and control.saved["version"] == 4,
        "Container readiness requires its original full-observation controller",
    )
    adapter.check()
    control.journal.check_engine()
    require(guard.device_uuid == adapter.target["uuid"], "Container readiness device changed")
    return {
        "host": {
            "boot_id": control.saved["host_processes"]["boot"],
            "uid": control.saved["operator"][0],
            "kernel": adapter.environment["artifacts"]["engine"]["kernel"],
        },
        "container": {
            "mounts": deepcopy(control.saved),
            "journal": deepcopy(control.journal.identity),
        },
    }


def validate_task(value, device):
    require(
        type(value) is dict
        and set(value) == {"container_id", "nonce", "cleanup"}
        and type(value["container_id"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", value["container_id"])
        and type(value["nonce"]) is str
        and re.fullmatch(r"[a-f0-9]{32}", value["nonce"]),
        "Startup maintenance did not identify its original container",
    )
    validate_cleanup(
        value["cleanup"],
        container_id=value["container_id"],
        nonce=value["nonce"],
        device_uuid=device,
    )


def task_identity(task):
    require(task.control.journal.load() is None, "Startup task left a pending original record")
    value = {
        "container_id": task.service.container_id,
        "nonce": task.status["nonce"],
        "cleanup": deepcopy(task.cleanup),
    }
    validate_task(value, task.target["uuid"])
    return value


def validate_previous(value, device):
    require(
        type(value) is dict
        and set(value) == {"had_record", "cleanup", "ready"}
        and type(value["had_record"]) is bool
        and value["ready"] is False,
        "Incomplete original-container recovery",
    )
    if not value["had_record"]:
        require(value["cleanup"] is None, "Unexpected original-container cleanup")
        return
    row = value["cleanup"]
    require(
        type(row) is dict
        and set(row) == {"container", "removed", "ready", "history"}
        and row["removed"] is True
        and row["ready"] is False
        and (
            row["container"] is None
            or type(row["container"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", row["container"])
        )
        and type(row["history"]) is str
        and re.fullmatch(re.escape(device) + r"\.[a-f0-9]{32}\.done\.json", row["history"]),
        "Original-container recovery lacks its matching archive",
    )


def stage_report(name, value, adapter):
    require(type(value) is dict, "Container startup stage report is missing")
    if name == "initial-recovery":
        require(
            set(value)
            == {
                "scope",
                "previous_job",
                "previous_reservation",
                "container_recovery",
                "maintenance",
                "environment",
                "recovered",
                "ready",
            }
            and value["scope"] == SCOPE
            and value["recovered"] is True
            and value["ready"] is False
            and value["environment"] == adapter.environment,
            "Initial container recovery did not verify the configured environment",
        )
        require(
            (value["previous_job"] is None) == (value["previous_reservation"] is None),
            "Incomplete previous container reservation",
        )
        if value["previous_job"] is not None:
            for key in ("previous_job", "previous_reservation"):
                require(
                    type(value[key]) is str and str(uuid.UUID(value[key])) == value[key],
                    "Invalid previous container reservation",
                )
        validate_previous(value["container_recovery"], adapter.target["uuid"])
        validate_task(value["maintenance"], adapter.target["uuid"])
        return
    require(
        set(value) == {"scope", "task", "report"} and value["scope"] == SCOPE,
        "Container startup stage scope changed",
    )
    validate_task(value["task"], adapter.target["uuid"])
    report = value["report"]
    require(
        type(report) is dict
        and type(report.get("challenge")) is str
        and re.fullmatch(r"[a-f0-9]{32}", report["challenge"]),
        "Container startup challenge is missing",
    )
    if name == "resources":
        resource_report(
            {key: item for key, item in report.items() if key != "challenge"},
            resource_request(report["challenge"]),
            target=adapter.target,
            selection=adapter.environment["device_spec"],
        )
    elif name.startswith("kernel/"):
        validate_check_report(report, check_request(name.split("/", 1)[1], report["challenge"]))
    else:
        require(name.startswith("controller/"), "Unknown container startup stage")
        validate_report(
            report,
            fault_request(name.split("/", 1)[1], report["challenge"]),
            device_uuid=adapter.target["uuid"],
            recovered=True,
        )
        require(
            all(report[key] == value["task"][key] for key in ("container_id", "nonce", "cleanup")),
            "Controller report changed its original maintenance task",
        )
