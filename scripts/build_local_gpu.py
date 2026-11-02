"""Build local control/task images without mapping a GPU or initializing CUDA."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import controller_digest  # noqa: E402
from judge.container_gpu import gpu_task_digest  # noqa: E402
from judge.container_practice_bundle import KIND, verify_images  # noqa: E402
from judge.container_preparation import create_json  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.gpu_session import decode_frame  # noqa: E402
from scripts.build_container_controller import build_controller  # noqa: E402
from scripts.build_container_driver import build_image  # noqa: E402


def build(args):
    output = args.output.absolute()
    if output.resolve() != output or os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("Use a new canonical build directory as the non-root operator")
    engine = LocalEngine(args.docker_socket)
    engine.verify()
    expected = {
        "controller_contract": controller_digest(),
        "task_contract": gpu_task_digest(),
        "execution_digest": execution_digest(),
    }
    output.mkdir(mode=0o700)
    control, task = output / "controller", output / "task"
    control.mkdir(mode=0o700)
    task.mkdir(mode=0o700)
    name = "praximesh-local-gpu-" + uuid.uuid4().hex
    controller_image = build_controller(
        engine,
        args.docker_cli_archive,
        control,
        name + "-control",
        nvml_library_dir=args.library_dir,
        full_inventory=True,
    )
    management = decode_frame((control / "context/observer/manifest.json").read_bytes())
    task_image, driver, _compiler = build_image(
        engine,
        args.runtime_archive,
        args.nvrtc_archive,
        args.library_dir,
        task,
        name + "-task",
        device_tasks=True,
    )
    value = {
        "format": 1,
        "kind": KIND,
        "controller_image": controller_image,
        "task_image": task_image,
        **expected,
        "management_manifest": management,
        "driver_manifest": driver,
    }
    verify_images(engine, value)
    create_json(output / "local-gpu.json", value)
    return {
        "manifest": str(output / "local-gpu.json"),
        "controller_image": controller_image,
        "task_image": task_image,
        "ready": False,
        "gpu_queried": False,
        "gpu_executed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--docker-cli-archive", required=True, type=Path)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--nvrtc-archive", required=True, type=Path)
    parser.add_argument(
        "--library-dir",
        required=True,
        type=Path,
        help="Trusted installed NVIDIA libraries; read/copied without loading",
    )
    parser.add_argument("--output", required=True, type=Path, help="New private output directory")
    args = parser.parse_args(argv)
    print(json.dumps(build(args), indent=2))


if __name__ == "__main__":
    main()
