"""Owned single-card execution component; not a worker or readiness receipt."""

from copy import deepcopy

from judge.container_connection import ContainerConnection
from judge.container_contract import PYTHON, WORKLOAD_PROFILE, resource_limits
from judge.container_devices import encoded_selection
from judge.container_gpu import GPUContainerService, gpu_task_digest
from judge.container_kernel_protocol import (
    check_prepared,
    checked_completion,
    prepare_request,
    prepared,
    validate_request,
)
from judge.container_monitor import ContainerDeviceMonitor
from judge.sandbox import SandboxUnavailable


class KernelTask:
    def __init__(
        self, control, job_id, reservation_token, *, image, driver_manifest, purpose="task"
    ):
        from judge.container_controller import ContainerController

        if type(control) is not ContainerController:
            raise ValueError("The original trusted container controller is required")
        control.check()
        if control.journal.load() is not None:
            raise SandboxUnavailable("Recover the previous task before kernel execution")
        self.control, self.journal = control, control.journal
        self.monitor = ContainerDeviceMonitor(control)
        selection = self.monitor.task_device_spec()
        self.target = deepcopy(self.monitor.saved["target"])
        self.identity = prepared(selection, driver_manifest, self.target)
        self.service = GPUContainerService(
            self.journal,
            job_id,
            reservation_token,
            image=image,
            driver_manifest=driver_manifest,
            device_spec=selection,
            purpose=purpose,
        )
        self.contract = gpu_task_digest()
        self.used = self.finishing = False
        self.status = self.record = self.cleanup = None

    def original_record(self):
        record = self.journal.load()
        if record is None:
            if self.service.container_id is not None:
                raise SandboxUnavailable("Started kernel task lost its original record")
            return None
        if (
            record["owner"] != self.service.owner
            or record["image"] != self.service.image
            or record["version"] != 3
            or record["resource_profile"] != WORKLOAD_PROFILE
            or record["device_spec"] != self.service.device_spec
            or record["container_id"] != self.service.container_id
            or self.record is not None
            and record != self.record
        ):
            raise SandboxUnavailable("Kernel task ownership changed; retain the record")
        return record

    def recover(self):
        if self.original_record() is None:
            return
        self.cleanup = self.journal.recover()
        if (
            self.cleanup is None
            or self.cleanup.get("removed") is not True
            or self.cleanup.get("ready") is not False
            or self.journal.load() is not None
        ):
            raise SandboxUnavailable("Kernel task cleanup was not confirmed")

    def run(self, request, *, deadline, on_begin, on_tick):
        if self.used:
            raise ValueError("A kernel task can only be invoked once")
        self.used = True
        request = deepcopy(validate_request(request))
        maintenance = request["event"] in {"recover", "check-kernel", "check-resources"}
        if self.service.purpose != ("maintenance" if maintenance else "task"):
            raise ValueError("Kernel request does not match its original purpose")
        connection = None

        def starting_tick():
            self.control.check()
            on_tick()
            self.monitor.observe()

        def tick():
            self.control.check()
            on_tick()
            self.original_record()
            if not self.finishing:
                self.monitor.observe(self.service)

        try:
            starting_tick()
            self.status = self.service.start(
                [
                    PYTHON,
                    "-I",
                    "/opt/praximesh/container_kernel_entry.py",
                    encoded_selection(self.service.device_spec),
                ],
                deadline=deadline,
                on_tick=starting_tick,
            )
            if (
                self.status["limits"] != resource_limits(WORKLOAD_PROFILE)
                or gpu_task_digest() != self.contract
            ):
                raise SandboxUnavailable("Kernel task image or resource identity changed")
            self.record = deepcopy(self.original_record())
            tick()
            connection = ContainerConnection(self.service, self.status, on_tick=tick)
            channel = connection.channel
            channel.send(prepare_request(self.target))
            check_prepared(channel.receive(), self.identity)
            tick()
            on_begin()
            tick()
            # Neither source nor protected fixtures cross the channel before
            # the caller's start transaction and original-record recheck.
            channel.send(request)
            completed = checked_completion(
                channel.receive(), request, target=self.target, selection=self.service.device_spec
            )
            tick()
            self.finishing = True
            channel.send({"event": "finish"})
            connection.finish()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self.recover()
        self.control.check()
        on_tick()
        # Discard the consumed task observer only after that original record
        # has been removed; a fresh idle observation never reclaims its PID.
        ContainerDeviceMonitor(self.control).observe()
        if gpu_task_digest() != self.contract:
            raise SandboxUnavailable("Kernel task source changed during execution")
        return {
            **completed,
            "artifacts": {
                "image": self.service.image,
                "task_contract": self.contract,
                "resource_profile": WORKLOAD_PROFILE,
                "limits": deepcopy(self.status["limits"]),
            },
        }
