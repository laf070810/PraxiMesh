"""Private local image selection, never a GPU readiness receipt or score."""

import json
import os
from copy import deepcopy

from judge.container_contract import ENTRY, PROTOCOL, PYTHON
from judge.container_controller import controller_digest
from judge.container_gpu import gpu_task_digest, image_contract
from judge.container_journal import MAX_RECORD, private
from judge.container_practice_controller import validate_image
from judge.container_practice_state import require
from judge.container_service import IMAGE_ID, PREFIX
from judge.driver_artifacts import validate_manifest as validate_driver
from judge.gpu_policy import execution_digest
from judge.gpu_session import decode_frame
from judge.nvml_artifacts import validate_manifest as validate_management

KIND = "praximesh-local-gpu-images-v1"
FIELDS = {
    "format",
    "kind",
    "controller_image",
    "task_image",
    "controller_contract",
    "task_contract",
    "execution_digest",
    "driver_manifest",
    "management_manifest",
}


def validate(value):
    require(type(value) is dict and set(value) == FIELDS, "Unknown local GPU image manifest")
    require(
        type(value["format"]) is int and value["format"] == 1 and value["kind"] == KIND,
        "Unknown local GPU image manifest version",
    )
    require(
        all(
            type(value[key]) is str and IMAGE_ID.fullmatch(value[key])
            for key in ("controller_image", "task_image")
        ),
        "Local GPU images must be immutable IDs",
    )
    validate_driver(value["driver_manifest"])
    validate_management(value["management_manifest"])
    require(
        value["management_manifest"]["schema"] == 2
        and value["management_manifest"]["driver_version"]
        == value["driver_manifest"]["driver_version"],
        "Local driver and full management bundle must match",
    )
    require(
        value["controller_contract"] == controller_digest()
        and value["task_contract"] == gpu_task_digest()
        and value["execution_digest"] == execution_digest(),
        "Local images belong to different sources; rebuild before a new invocation",
    )
    return deepcopy(value)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        private(os.fstat(stream.fileno()))
        raw = stream.read(MAX_RECORD + 1)
    require(len(raw) <= MAX_RECORD, "Local image manifest exceeded its bound")
    return validate(decode_frame(raw))


def verify_images(engine, value, *, task=True):
    """Inspect local immutable images without creating a container or reading a GPU."""
    value = validate(value)
    for role in ("controller", "task") if task else ("controller",):
        image_id = value[role + "_image"]
        images = json.loads(engine.run("image", "inspect", image_id).stdout)
        require(
            type(images) is list
            and len(images) == 1
            and images[0]["Id"] == image_id
            and images[0]["Os"] == "linux"
            and images[0]["Architecture"] == "amd64",
            "Selected local image is unavailable or incompatible",
        )
        config = images[0]["Config"]
        if role == "controller":
            validate_image(config, value["controller_contract"], value["management_manifest"])
        else:
            labels = config.get("Labels") or {}
            require(
                type(labels) is dict
                and labels.get(PREFIX + "protocol") == PROTOCOL
                and labels.get(PREFIX + "runtime") == image_contract(value["driver_manifest"])
                and config.get("Entrypoint") == [PYTHON, "-I", ENTRY]
                and type(config.get("Env")) is list
                and all(type(item) is str for item in config["Env"])
                and not any(config.get(key) for key in ("Volumes", "OnBuild", "Healthcheck")),
                "Task image differs from the fixed GPU supervisor contract",
            )
    return value
