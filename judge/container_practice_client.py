"""One bounded local invocation through the fixed original control container."""

import os
import signal
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy

from judge.container_adapter import configured_environment
from judge.container_devices import PROFILE, kernel_uvm_major
from judge.container_practice_controller import ControllerInvocation, stopped, timestamp
from judge.container_practice_host import HostWorkspace
from judge.container_practice_protocol import (
    MAX_SECONDS_TOTAL,
    MODES,
    configuration,
    exchange,
    same,
)
from judge.container_practice_state import require
from judge.container_practice_transport import PipeChannel
from judge.container_processes import read_file
from judge.container_service import IMAGE_ID
from judge.gpu_command import MAX_ERROR
from judge.gpu_task_protocol import public_task_request
from judge.sandbox import SandboxUnavailable

MAINTENANCE_SECONDS = 60


@contextmanager
def deferred_signals():
    cancelled = False

    def stop(_number, _frame):
        nonlocal cancelled
        cancelled = True

    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)

        def check():
            if cancelled:
                raise InterruptedError("Local GPU operation cancelled; retain original state")

        yield check
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def expected_environment(host, image, driver):
    """No driver initialization; the child independently observes this selection."""
    host.check()
    observer = host.mounts["device_observer"]
    fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        uvm_major = kernel_uvm_major(read_file(fd, "devices", 65536))
    finally:
        os.close(fd)
    selection = {
        "schema": 1,
        "profile": PROFILE,
        "uuid": host.device,
        "driver": observer["target"]["driver"],
        "minor": observer["minor"],
        "uvm_major": uvm_major,
    }
    value = configured_environment(
        observer["target"],
        selection,
        image=image,
        driver_manifest=driver,
        management_manifest=observer["library"],
        engine=host.engine.verify(),
    )
    host.check()
    return value


class AttachedController:
    def __init__(self, control, on_tick):
        require(
            type(control) is ControllerInvocation and control.fresh,
            "A fresh original controller is required",
        )
        self.control, self.cancelled = control, on_tick
        self.process = None
        self.errors = 0
        self.deadline = control.record["hello"]["deadline"]
        # docker start does not write configuration. Keep its explicit config
        # path absent under the original private directory, even after SIGKILL.
        # Any existing file/directory is retained and rejected, never removed.
        self.config = (
            control.host.root / "journal" / ("local-docker-config-" + control.record["nonce"])
        )

    def configuration_absent(self):
        try:
            self.config.lstat()
        except FileNotFoundError:
            return
        raise SandboxUnavailable("Unexpected local Docker configuration; retain original state")

    def drain_errors(self):
        if self.process is None:
            return
        while True:
            try:
                data = os.read(self.process.stderr.fileno(), 8192)
            except BlockingIOError:
                break
            if not data:
                break
            self.errors += len(data)
            require(
                self.errors <= MAX_ERROR, "Attached controller diagnostics exceeded their bound"
            )

    def check(self):
        self.cancelled()
        require(time.monotonic() < self.deadline, "Original local invocation expired")
        self.configuration_absent()
        self.control.check_fresh()
        self.drain_errors()
        info = self.control.observe()
        self.cancelled()
        require(
            time.monotonic() < self.deadline, "Original local invocation expired during inspection"
        )
        if self.process is not None:
            require(self.process.poll() in (None, 0), "Original attached process failed")
        return info

    def __enter__(self):
        self.cancelled()
        self.configuration_absent()
        original = self.control.request_start()
        self.configuration_absent()
        self.cancelled()
        try:
            self.process = subprocess.Popen(
                [
                    "docker",
                    "--config",
                    str(self.config),
                    "--host",
                    "unix://" + str(self.control.host.engine.path),
                    "container",
                    "start",
                    "--attach",
                    "--interactive",
                    original,
                ],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            os.set_blocking(self.process.stderr.fileno(), False)
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def success(self):
        while self.process.poll() is None:
            self.check()
            try:
                self.process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        info = self.check()
        state = info["State"]
        require(
            self.process.returncode == 0
            and stopped(info)
            and state["Status"] == "exited"
            and type(state.get("ExitCode")) is int
            and state["ExitCode"] == 0
            and state.get("OOMKilled") is False
            and state.get("Error") == ""
            and self.control.record["started_at"] is not None
            and timestamp(state.get("FinishedAt"))
            not in {"0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000000Z"},
            "Original control container did not finish successfully",
        )
        return info

    def close(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise SandboxUnavailable("Original attached process did not terminate") from None
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()

    def __exit__(self, *_error):
        self.close()


def completed_operation(mode, before, after, result, environment):
    """Confirm persistent inner state after the original outer process is gone."""
    if mode == "run":
        require(
            after is not None
            and after["phase"] == "completed"
            and same(after["environment"], environment)
            and (
                before is None or all(after[key] != before[key] for key in ("job", "reservation"))
            ),
            "Public completion did not produce a new matching terminal operation",
        )
    elif mode == "recover" and before is not None and before["phase"] == "pending":
        require(
            result["status"] == "recovered" and same(after, {**before, "phase": "recovered"}),
            "Original inner recovery is not confirmed",
        )
    else:
        require(same(after, before), "Maintenance changed the original inner operation")
        require(
            result["status"] == ("initialized" if mode == "initialize" else "idle"),
            "Unexpected local maintenance result",
        )


def invoke(
    host,
    controller_image,
    mode,
    *,
    image=None,
    driver=None,
    problem=None,
    source=None,
    on_tick=lambda: None,
    on_progress=None,
):
    """Return only after protocol, process, container and durable state agree."""
    require(type(host) is HostWorkspace, "Original local host workspace is required")
    require(
        type(controller_image) is str and IMAGE_ID.fullmatch(controller_image),
        "An immutable controller image is required",
    )
    require(type(mode) is str and mode in MODES, "Unknown local operation")
    environment = None
    if mode == "run":
        public_task_request(problem, source)
        configuration(image, driver)
    else:
        require(
            all(value is None for value in (image, driver, problem, source)),
            "Maintenance cannot carry learner input",
        )
    on_tick()
    control = ControllerInvocation(host)
    if mode == "recover" and control.record is not None:
        control.remove()
    elif control.record is not None:
        require(control.record["phase"] == "completed", "Recover the original invocation first")
        control.remove()  # A previously completed controller must still be absent.
    if mode == "run":
        environment = expected_environment(host, image, driver)
    deadline = time.monotonic() + (MAX_SECONDS_TOTAL if mode == "run" else MAINTENANCE_SECONDS)
    before = control.prepare(controller_image, mode, deadline)
    try:
        on_tick()
        control.create()
        with AttachedController(control, on_tick) as attached:
            with PipeChannel(
                attached.process.stdout.fileno(),
                attached.process.stdin.fileno(),
                deadline=deadline,
                on_tick=attached.check,
            ) as channel:
                result = exchange(
                    channel,
                    control.record["hello"],
                    image=image,
                    driver=driver,
                    environment=environment,
                    problem=problem,
                    source=source,
                    on_progress=on_progress,
                )
            attached.success()
        control.remove()
        with host.physical() as state:
            on_tick()
            control.check_fresh()
            require(
                control.record["phase"] == "removed" and state.journal.load() is None,
                "Original local containers remain active",
            )
            completed_operation(mode, before, state.operation(), result, environment)
            control.save("completed")
            on_tick()
            control.check_fresh()
        return deepcopy(result)
    except BaseException:
        # Cleanup must not depend on cancelled pipes or the elapsed run budget.
        # It only stops this frozen outer container; inner recovery is explicit.
        try:
            control.remove()
        except Exception as error:
            raise SandboxUnavailable(
                "Original controller cleanup is unconfirmed; retain state for explicit recovery"
            ) from error
        raise
