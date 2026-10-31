"""One prepared kernel task; no environment receipt or readiness publication."""

import json
from copy import deepcopy

from judge.container_contract import WORKLOAD_PROFILE, resource_limits
from judge.container_devices import validate_selection
from judge.driver_artifacts import manifest_digest
from judge.gpu_artifacts import RUNSC_SHA256
from judge.gpu_task_protocol import task_request, validate_result, validate_target
from judge.sandbox import SandboxUnavailable

PROTOCOL = "praximesh-container-kernel-v1"
FINISH_MARGIN = 5


def prepare_request(target):
    validate_target(target)
    return {"event": "prepare-kernel", "protocol": PROTOCOL, "target": deepcopy(target)}


def prepared(selection, manifest, target):
    validate_selection(selection)
    validate_target(target)
    if selection["uuid"] != target["uuid"] or selection["driver"] != target["driver"]:
        raise SandboxUnavailable("Kernel target does not match the selected device")
    if manifest["driver_version"] != selection["driver"]:
        raise SandboxUnavailable("Kernel driver does not match the selected device")
    return {
        "event": "kernel-ready",
        "protocol": PROTOCOL,
        "device_spec": deepcopy(selection),
        "target": deepcopy(target),
        "driver_manifest": manifest_digest(manifest),
        "runsc_sha256": RUNSC_SHA256,
        "resource_profile": WORKLOAD_PROFILE,
        "limits": resource_limits(WORKLOAD_PROFILE),
    }


def validate_request(value):
    if value == {"event": "recover"}:
        return value
    if type(value) is not dict or set(value) != {"event", "source", "problem", "mode"}:
        raise ValueError("Unexpected kernel task request")
    if value != task_request(value["problem"], value["source"], value["mode"]):
        raise ValueError("Unexpected kernel task data")
    return value


def check_prepared(value, expected):
    # JSON identity must also distinguish booleans/floats from integer fields.
    try:
        valid = json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True, allow_nan=False
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise SandboxUnavailable("Kernel preparation identity changed")


def expected_cleanup(request):
    labels = ["device-recovery"] if request["event"] == "recover" else ["task", "device-recovery"]
    return [{"id": name, "stopped": True, "removed": True} for name in labels]


def checked_completion(value, request):
    fields = {"event", "protocol", "recovered", "cleanup", "ready"}
    maintenance = request["event"] == "recover"
    if not maintenance:
        fields.add("result")
    if (
        type(value) is not dict
        or set(value) != fields
        or value["event"] != "kernel-completed"
        or value["protocol"] != PROTOCOL
        or value["recovered"] is not True
        or value["ready"] is not False
        or value["cleanup"] != expected_cleanup(request)
        or any(
            item.get(key) is not True for item in value["cleanup"] for key in ("stopped", "removed")
        )
    ):
        raise SandboxUnavailable("Kernel task did not confirm independent recovery and cleanup")
    if not maintenance:
        validate_result(value["result"], request["problem"], request["mode"])
    return deepcopy(value)
