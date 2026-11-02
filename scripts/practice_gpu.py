"""Local GPU CLI orchestration, using original state and the fixed controller."""

import sys

from judge.container_practice_bundle import read, verify_images
from judge.container_practice_client import deferred_signals, invoke
from judge.container_practice_host import HostWorkspace
from judge.container_preparation import initialize_state
from judge.container_service import LocalEngine


def run_local(args, *, problem=None, source=None):
    # Public package validation belongs to practice.py, before any engine/state access.
    bundle = read(args.gpu_bundle)
    engine = LocalEngine(args.docker_socket)
    verify_images(engine, bundle, task=args.command in {"prepare-gpu", "test-gpu"})
    with deferred_signals() as cancelled:
        cancelled()
        if args.command == "prepare-gpu":
            initialize_state(args.state, engine, args.gpu, bundle["management_manifest"])
            cancelled()
            return {
                "status": "prepared",
                "state": str(args.state),
                "ready": False,
                "formal_result": False,
            }
        mode = {"initialize-gpu": "initialize", "test-gpu": "run", "recover-gpu": "recover"}[
            args.command
        ]
        with HostWorkspace(args.state, engine, initialize=mode == "initialize") as host:
            options = {}
            if mode == "run":
                options = {
                    "image": bundle["task_image"],
                    "driver": bundle["driver_manifest"],
                    "problem": problem,
                    "source": source,
                }
            result = invoke(
                host,
                bundle["controller_image"],
                mode,
                on_tick=cancelled,
                on_progress=lambda name: print(
                    "Local GPU startup: " + name, file=sys.stderr, flush=True
                ),
                **options,
            )
            return result
