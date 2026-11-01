"""Frozen GPU release metadata and build support; no device access or activation."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROLES = {"gpu_worker", "gpu_task"}
SCOPES = {"execution", "controller", "worker", "task"}
HEX = re.compile(r"[a-f0-9]{64}")
# These describe release schema 1, not the current contents of the working tree.
REQUIRED = {
    "execution": {"judge/gpu_policy.py", "core/gpu_worker.py", "core/gpu_readiness.py"},
    "controller": {"judge/container_controller.py", "judge/container_monitor.py"},
    "worker": {
        "scripts/container_worker_entry.py",
        "core/management/commands/container_gpu_worker.py",
        "praximesh/container_worker_settings.py",
        "judge/container_worker_image.py",
        "requirements.lock",
        "uv.lock",
    },
    "task": {"judge/container_gpu.py", "deploy/container-gpu.fragment"},
}


def validate_metadata(value, files):
    from judge.container_worker_image import APP_DIRECTORIES
    from judge.driver_artifacts import validate_manifest as validate_driver
    from judge.nvml_artifacts import validate_manifest as validate_management

    if (
        type(value) is not dict
        or set(value) != {"schema", "profile", "contracts", "source_scopes", "driver", "management"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["profile"] != "container-single-card-v1"
        or type(value["contracts"]) is not dict
        or set(value["contracts"]) != SCOPES
        or any(type(v) is not str or not HEX.fullmatch(v) for v in value["contracts"].values())
        or type(value["source_scopes"]) is not dict
        or set(value["source_scopes"]) != SCOPES
    ):
        raise ValueError("Invalid GPU release identity")
    scopes = value["source_scopes"]
    for name, names in scopes.items():
        if (
            type(names) is not list
            or not 1 <= len(names) <= 2000
            or any(type(n) is not str or n not in files for n in names)
            or len(set(names)) != len(names)
            or not REQUIRED[name] <= set(names)
        ):
            raise ValueError("Invalid frozen GPU source scope")
    if not (
        set(scopes["execution"]) | set(scopes["task"]) <= set(scopes["controller"])
        and set(scopes["controller"]) <= set(scopes["worker"])
    ):
        raise ValueError("GPU source scopes are inconsistent")
    application = {
        name for name in files if name.endswith(".py") and name.split("/", 1)[0] in APP_DIRECTORIES
    }
    if not application <= set(scopes["worker"]):
        raise ValueError("GPU worker scope omits application source")
    validate_driver(value["driver"])
    validate_management(value["management"])
    if (
        value["management"]["schema"] != 2
        or value["driver"]["driver_version"] != value["management"]["driver_version"]
    ):
        raise ValueError("GPU task and complete management versions differ")
    return set().union(*map(set, scopes.values()))


def verify_contracts(value, contents):
    from judge.driver_artifacts import manifest_digest

    for name, paths in value["source_scopes"].items():
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.encode() + b"\0" + contents[path])
        result = digest.hexdigest()
        if name == "task":
            result = hashlib.sha256(
                b"praximesh-single-gpu-task-v1\0"
                + result.encode()
                + b"\0"
                + manifest_digest(value["driver"]).encode()
            ).hexdigest()
        if result != value["contracts"][name]:
            raise ValueError("Archived GPU sources differ from their contract")


def check_image(role, config, value):
    """Check the actual Docker config, either archived or already imported."""
    from judge.nvml_artifacts import manifest_digest

    if type(config) is not dict:
        raise ValueError("GPU image configuration must be an object")
    labels = config.get("Labels") or {}
    if role == "gpu_worker":
        user = "0:0"
        entry = ["/usr/local/bin/python", "-I", "/opt/praximesh/scripts/container_worker_entry.py"]
        expected = {
            "io.praximesh.controller.contract": value["contracts"]["controller"],
            "io.praximesh.worker.contract": value["contracts"]["worker"],
            "io.praximesh.controller.observer": manifest_digest(value["management"]),
        }
    elif role == "gpu_task":
        user = "65532:65532"
        entry = ["/usr/local/bin/python", "-I", "/opt/praximesh/container_entry.py"]
        expected = {
            "io.praximesh.supervision.protocol": "praximesh-container-supervision-v1",
            "io.praximesh.supervision.runtime": value["contracts"]["task"],
        }
    else:
        raise ValueError("Unknown GPU image role")
    if (
        config.get("User") != user
        or config.get("Entrypoint") != entry
        or config.get("Cmd") not in (None, [])
        or type(labels) is not dict
        or any(labels.get(key) != expected_value for key, expected_value in expected.items())
    ):
        raise ValueError("GPU image role, entrypoint or contract mismatch")


def check_engine(options, docker):
    from judge.container_service import LocalEngine

    engine = LocalEngine(options["docker_socket"])
    info = engine.verify()
    current = docker("info", "--format", "{{.ID}}", capture_output=True).stdout.decode().strip()
    if current != info["ID"]:
        raise ValueError("CPU and GPU releases must use the same explicit rootless engine")
    return engine


@contextmanager
def build_images(context, work, options, docker, log_directory):
    """Run only the reviewed snapshot's builder; callers never run archived code."""
    engine = check_engine(options, docker)
    output = work / "gpu-build"
    tag = "px-gpu-release-" + uuid.uuid4().hex[:12]
    try:
        with (log_directory / "entry.log").open("w") as log:
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(context / "scripts/release_gpu.py"),
                    "--output",
                    str(output),
                    "--tag",
                    tag,
                    *[
                        part
                        for name, path in options.items()
                        for part in ("--" + name.replace("_", "-"), str(path))
                    ],
                ],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=2500,
            )
        raw = (output / "release-images.json").read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError("GPU build metadata is oversized")
        yield json.loads(raw)
    finally:
        for relative, name in (("build.log", "worker.log"), ("task/build.log", "task.log")):
            path = output / relative
            if path.is_file():
                shutil.copyfile(path, log_directory / name)
        for name in (tag + "-task", tag):
            if engine.run("image", "inspect", name, check=False).returncode == 0:
                engine.run("image", "rm", name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "output",
        "docker-socket",
        "docker-cli-archive",
        "runtime-archive",
        "nvrtc-archive",
        "management-library-dir",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    from judge.container_controller import CONTROLLER_SOURCES, controller_digest
    from judge.container_gpu import SOURCES as TASK_SOURCES
    from judge.container_gpu import image_contract
    from judge.container_service import LocalEngine
    from judge.container_worker_image import source_files, worker_digest
    from judge.gpu_policy import EXECUTION_SOURCES, execution_digest
    from scripts.build_container_driver import build_image
    from scripts.build_container_worker import build_worker

    args.output.mkdir(mode=0o700)
    engine = LocalEngine(args.docker_socket)
    worker = build_worker(
        engine, args.docker_cli_archive, args.management_library_dir, args.output, args.tag
    )
    task_output = args.output / "task"
    task_output.mkdir(mode=0o700)
    task, driver, _compiler = build_image(
        engine,
        args.runtime_archive,
        args.nvrtc_archive,
        args.management_library_dir,
        task_output,
        args.tag + "-task",
        device_tasks=True,
    )
    metadata = {
        "schema": 1,
        "profile": "container-single-card-v1",
        "contracts": {
            "execution": execution_digest(),
            "controller": controller_digest(),
            "worker": worker_digest(),
            "task": image_contract(driver),
        },
        "source_scopes": {
            "execution": list(EXECUTION_SOURCES),
            "controller": list(CONTROLLER_SOURCES),
            "worker": list(source_files()),
            "task": list(TASK_SOURCES),
        },
        "driver": driver,
        "management": worker["management_manifest"],
    }
    if (
        worker["worker_contract"] != metadata["contracts"]["worker"]
        or worker["controller_contract"] != metadata["contracts"]["controller"]
    ):
        raise ValueError("GPU source changed between image builds")
    (args.output / "release-images.json").write_text(
        json.dumps(
            {"images": {"gpu_worker": worker["image"], "gpu_task": task}, "gpu": metadata},
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
