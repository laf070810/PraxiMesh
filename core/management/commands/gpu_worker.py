"""Explicit host worker; no fallback, saved-report import or web registration."""

import re
from contextlib import ExitStack
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.gpu_worker import StopFlag, run_worker, stop_signals
from judge.container_journal import ContainerJournal
from judge.container_service import LocalEngine
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import GPU_UUID, DeviceLock
from judge.gpu_monitor import identify


class Command(BaseCommand):
    help = (
        "Run a supervised GPU worker with full startup checks on an explicitly allocated, "
        "idle device. Requires host user services and pinned offline artifacts."
    )

    def add_arguments(self, parser):
        for option, description in (
            ("gpu", "Full UUID of the explicitly allocated, idle GPU."),
            ("worker-id", "Stable worker name, 1-80 letters, digits or _ . : -."),
            ("runtime-archive", "Local pinned gVisor runtime archive."),
            ("image-archive", "Local pinned OCI image archive."),
            ("nvrtc-archive", "Local pinned NVRTC compiler archive."),
            ("podman-version", "Exact installed Podman X.Y.Z version."),
            ("directory", "Absolute private persistent job workspace root."),
            ("lock-directory", "Absolute private lock directory shared by every controller."),
            ("receipt-directory", "Absolute private directory for current-owner receipts."),
        ):
            parser.add_argument("--" + option, required=True, help=description)
        parser.add_argument(
            "--once", action="store_true", help="Full startup, then at most one claim."
        )
        parser.add_argument(
            "--container-journal",
            help="Existing private journal to recover before GPU startup checks.",
        )
        parser.add_argument(
            "--docker-socket", help="Explicit rootless socket used by --container-journal."
        )

    def handle(self, *args, **options):
        if not settings.JUDGE_ENABLED:
            raise CommandError("JUDGE_ENABLED is off. No GPU work will be started.")
        if bool(options["container_journal"]) != bool(options["docker_socket"]):
            raise CommandError(
                "--container-journal and --docker-socket must be configured together."
            )
        if options["container_journal"]:
            path = Path(options["container_journal"])
            if (
                not path.is_absolute()
                or ".." in path.parts
                or not (path / "installation.json").is_file()
            ):
                raise CommandError(
                    "--container-journal requires an existing initialized absolute directory."
                )
        if not GPU_UUID.fullmatch(options["gpu"]):
            raise CommandError("--gpu requires a full GPU UUID, not an index or 'all'.")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", options["worker_id"]):
            raise CommandError("--worker-id must contain 1-80 letters, digits or _ . : -.")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", options["podman_version"]):
            raise CommandError("--podman-version requires the exact X.Y.Z version.")
        for name in ("directory", "lock_directory", "receipt_directory"):
            path = Path(options[name])
            if not path.is_absolute() or ".." in path.parts:
                raise CommandError("GPU private directories must use absolute paths without '..'.")
        for name in ("runtime_archive", "image_archive", "nvrtc_archive"):
            if not Path(options[name]).is_file():
                raise CommandError("Every pinned GPU archive must exist as a local file.")
        stop = StopFlag()
        try:
            with (
                stop_signals(stop),
                DeviceLock(options["lock_directory"], options["gpu"]) as guard,
                ExitStack() as owned,
            ):
                journal = None
                if options["container_journal"]:
                    journal = owned.enter_context(
                        ContainerJournal(
                            options["container_journal"],
                            guard,
                            LocalEngine(options["docker_socket"]),
                        )
                    )
                adapter = GPUExecutionAdapter(
                    target=identify(guard.device_uuid),
                    directory=options["directory"],
                    runtime_archive=options["runtime_archive"],
                    image_archive=options["image_archive"],
                    nvrtc_archive=options["nvrtc_archive"],
                    podman_version=options["podman_version"],
                )
                run_worker(
                    guard,
                    options["worker_id"],
                    adapter,
                    receipt_directory=options["receipt_directory"],
                    stop=stop,
                    once=options["once"],
                    container_journal=journal,
                )
        except Exception as error:
            # Never copy raw process/DB errors, learner output or credentials.
            raise CommandError("GPU worker stopped: " + type(error).__name__) from None
