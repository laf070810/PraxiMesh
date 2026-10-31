"""Bounded compilation-only messages; no execution, verdict or readiness claim."""

import re

from judge.gpu_contract import compile_request
from judge.sandbox import SandboxUnavailable

PROTOCOL = "praximesh-cuda-compile-v1"


def architecture(value):
    if (
        type(value) is not list
        or len(value) != 2
        or any(type(part) is not int for part in value)
        or not 1 <= value[0] <= 99
        or not 0 <= value[1] <= 9
    ):
        raise ValueError("An explicit CUDA compute architecture is required")
    return list(value)


def request(source, operation, target):
    compile_request(source, operation)
    return {
        "event": "compile",
        "protocol": PROTOCOL,
        "source": source,
        "operation": operation,
        "architecture": architecture(target),
    }


def validate_request(value):
    if type(value) is not dict or set(value) != {
        "event",
        "protocol",
        "source",
        "operation",
        "architecture",
    }:
        raise ValueError("Unexpected compilation-only request")
    if value != request(value["source"], value["operation"], value["architecture"]):
        raise ValueError("Unexpected compilation-only protocol")
    return value


def validate_identity(value):
    if (
        type(value) is not dict
        or value
        != {
            "event": "compiler-ready",
            "nvrtc_version": [13, 1],
            "gpu_nodes": [],
            "cuda_driver_available": False,
        }
        or value["cuda_driver_available"] is not False
        or any(type(part) is not int for part in value["nvrtc_version"])
    ):
        raise SandboxUnavailable("Unexpected device-free compiler identity")


def validate_compiled(value, target):
    if (
        type(value) is not dict
        or set(value) != {"event", "architecture", "ptx_bytes", "ptx_sha256"}
        or value["event"] != "compiled"
        or value["architecture"] != architecture(target)
        or any(type(part) is not int for part in value["architecture"])
        or type(value["ptx_bytes"]) is not int
        or not 1 <= value["ptx_bytes"] <= 4 * 1024 * 1024
        or type(value["ptx_sha256"]) is not str
        or not re.fullmatch("[a-f0-9]{64}", value["ptx_sha256"])
    ):
        raise SandboxUnavailable("Unexpected bounded PTX metadata")
