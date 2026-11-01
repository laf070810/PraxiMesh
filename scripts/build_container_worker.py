"""Build the trusted application worker without mapping or querying a GPU."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import controller_digest  # noqa: E402
from judge.container_service import IMAGE_ID, LocalEngine  # noqa: E402
from judge.container_worker_image import source_files, worker_digest  # noqa: E402
from judge.nvml_artifacts import manifest_digest  # noqa: E402
from scripts.build_container_controller import stage_cli  # noqa: E402
from scripts.build_container_monitor import snapshot_management, verify_installed  # noqa: E402


def build_worker(engine, archive, library_dir, output, tag):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,100}", tag):
        raise ValueError("Use an explicit local worker image tag")
    engine.verify()
    expected, controller = worker_digest(), controller_digest()
    context = output / "context"
    context.mkdir(mode=0o700)
    stage_cli(archive, context / "docker")
    for name in source_files():
        target = context / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    if worker_digest(context / "source") != expected:
        raise ValueError("Worker source changed during staging")
    observer = snapshot_management(library_dir, context / "observer", full_inventory=True)
    shutil.copyfile(context / "source/deploy/container-worker.Dockerfile", context / "Dockerfile")
    client = output / "docker-config"
    client.mkdir(mode=0o700)
    with (output / "build.log").open("w") as log:
        subprocess.run(
            [
                "docker",
                "--config",
                str(client),
                "--host",
                "unix://" + str(engine.path),
                "build",
                "--tag",
                tag,
                "--iidfile",
                str(output / "image-id"),
                "--build-arg",
                "CONTROLLER_CONTRACT=" + controller,
                "--build-arg",
                "WORKER_CONTRACT=" + expected,
                "--build-arg",
                "OBSERVER_CONTRACT=" + manifest_digest(observer),
                str(context),
            ],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )
    image = (output / "image-id").read_text().strip()
    if (
        not IMAGE_ID.fullmatch(image)
        or worker_digest() != expected
        or controller_digest() != controller
    ):
        raise ValueError("Worker image or source identity changed during build")
    verify_installed(library_dir, observer)
    result = {
        "schema": 1,
        "image": image,
        "worker_contract": expected,
        "controller_contract": controller,
        "management_manifest": observer,
    }
    (output / "worker-image.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    (output / "worker-image.json").chmod(0o600)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--docker-cli-archive", required=True, type=Path)
    parser.add_argument("--management-library-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--runtime-archive",
        type=Path,
        help="Also build the fixed single-card task image; pair with --nvrtc-archive.",
    )
    parser.add_argument("--nvrtc-archive", type=Path)
    args = parser.parse_args()
    try:
        if bool(args.runtime_archive) != bool(args.nvrtc_archive):
            raise ValueError("Runtime and NVRTC archives must be supplied together")
        output = args.output.absolute()
        if output.resolve() != output:
            raise ValueError("Build output must not contain links")
        output.mkdir(mode=0o700)
        result = build_worker(
            LocalEngine(args.docker_socket),
            args.docker_cli_archive,
            args.management_library_dir,
            output,
            args.tag,
        )
        if args.runtime_archive is not None:
            from scripts.build_container_driver import build_image

            task = output / "task"
            task.mkdir(mode=0o700)
            image, driver, _compiler = build_image(
                LocalEngine(args.docker_socket),
                args.runtime_archive,
                args.nvrtc_archive,
                args.management_library_dir,
                task,
                args.tag + "-task",
                device_tasks=True,
            )
            result.update(task_image=image, driver_manifest=driver)
            (output / "worker-image.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n"
            )
        print(json.dumps({"image": result["image"], "worker_contract": result["worker_contract"]}))
    except Exception as error:
        print("Container worker build failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
