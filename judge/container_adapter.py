"""Container GPU execution and identity; complete startup admission remains separate."""

from copy import deepcopy
from uuid import uuid4

from judge import gpu_artifacts
from judge.container_controller import (
    CLI_SHA256,
    CLI_VERSION,
    LIMITS,
    ContainerController,
    controller_digest,
)
from judge.container_environment import engine_identity, environment_identity
from judge.container_execution import KernelTask
from judge.container_gpu import gpu_task_digest
from judge.container_monitor import ContainerDeviceMonitor
from judge.container_runtime_service import contract_digest
from judge.container_service import IMAGE_ID
from judge.driver_artifacts import manifest_digest, validate_manifest
from judge.gpu_execution_contract import canonical_digest
from judge.gpu_kernel_checks import check_request
from judge.gpu_policy import execution_digest
from judge.gpu_task_protocol import task_request
from judge.sandbox import SandboxUnavailable


class ContainerGPUAdapter:
    def __init__(self, control, *, image, driver_manifest):
        if type(control) is not ContainerController:
            raise ValueError("The original trusted container controller is required")
        if type(image) is not str or not IMAGE_ID.fullmatch(image):
            raise ValueError("An immutable local GPU task image is required")
        self.control, self.image = control, image
        self.driver_manifest = deepcopy(validate_manifest(driver_manifest))
        self.environment = self.snapshot()
        self.target = deepcopy(self.environment["target"])
        self.fingerprint = canonical_digest(self.environment)

    def snapshot(self):
        self.control.check()
        monitor = ContainerDeviceMonitor(self.control)
        selection = monitor.task_device_spec()
        artifacts = {
            "task_image": self.image,
            "task_contract": gpu_task_digest(),
            "runtime_contract": contract_digest(),
            "controller_contract": controller_digest(),
            "driver_manifest": self.driver_manifest,
            "management_manifest": monitor.saved["library"],
            "runsc_sha256": gpu_artifacts.RUNSC_SHA256,
            "runsc_release": gpu_artifacts.RELEASE,
            "nvrtc_archive_sha256": gpu_artifacts.NVRTC_ARCHIVE_SHA256,
            "nvrtc_libraries": dict(gpu_artifacts.LIBRARIES),
            "docker_cli_version": CLI_VERSION,
            "docker_cli_sha256": CLI_SHA256,
            "engine": engine_identity(self.control.engine.verify()),
            "controller_limits": LIMITS,
        }
        return environment_identity(
            monitor.saved["target"], selection, artifacts, execution_digest=execution_digest()
        )

    def check(self):
        if (
            canonical_digest(self.snapshot()) != self.fingerprint
            or canonical_digest(self.environment) != self.fingerprint
            or self.target != self.environment["target"]
        ):
            raise SandboxUnavailable("Container GPU environment changed after preparation")

    def task(self, guard, job_id, reservation_token, *, purpose="task"):
        if guard is not self.control.guard:
            raise ValueError("Container execution requires its original device lock")
        self.check()
        return KernelTask(
            self.control,
            job_id,
            reservation_token,
            image=self.image,
            driver_manifest=self.driver_manifest,
            purpose=purpose,
        )

    def exchange(self, task, request, *, deadline, on_tick, on_begin):
        if (
            type(task) is not KernelTask
            or task.control is not self.control
            or task.service.image != self.image
            or manifest_digest(task.service.driver_manifest)
            != manifest_digest(self.driver_manifest)
            or task.target != self.target
        ):
            raise ValueError("The original configured kernel task is required")
        self.check()

        def begin():
            on_begin()
            self.check()

        completed = task.run(request, deadline=deadline, on_tick=on_tick, on_begin=begin)
        expected = {
            "image": self.image,
            "task_contract": self.environment["artifacts"]["task_contract"],
            "resource_profile": self.environment["resource_profile"],
            "limits": self.environment["workload_limits"],
        }
        if canonical_digest(completed["artifacts"]) != canonical_digest(expected):
            raise SandboxUnavailable("Completed GPU task changed its configured environment")
        self.check()
        on_tick()
        return completed

    def evaluate(self, task, *, deadline, problem, source, mode, on_tick, on_begin):
        completed = self.exchange(
            task,
            task_request(problem, source, mode),
            deadline=deadline,
            on_tick=on_tick,
            on_begin=on_begin,
        )
        return deepcopy(completed["result"]), deepcopy(self.environment)

    def recover(self, task, *, deadline, on_tick):
        self.exchange(
            task,
            {"event": "recover"},
            deadline=deadline,
            on_tick=on_tick,
            on_begin=lambda: None,
        )
        return deepcopy(self.environment)

    def check_kernel(self, task, name, *, deadline, on_tick):
        completed = self.exchange(
            task,
            check_request(name, uuid4().hex),
            deadline=deadline,
            on_tick=on_tick,
            on_begin=lambda: None,
        )
        return deepcopy(completed["report"]), deepcopy(self.environment)
