"""Pinned NVIDIA metadata and original-task PID checks in a trusted controller."""

import copy
import os
import re
import stat
import sys
import time
from pathlib import Path

from judge.container_devices import PROFILE, kernel_uvm_major, validate_selection
from judge.container_processes import MAX_PIDS, OwnedProcesses, read_file
from judge.driver_artifacts import VERSION, version_from_kernel
from judge.gpu_command import run_command
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_session import decode_frame
from judge.nvml_artifacts import DIRECTORY, manifest_digest, validate_manifest, verify_bundle
from judge.sandbox import SandboxUnavailable
from judge.smi_inventory import MAX_XML, inventory, utility_version


def validate_observer(value):
    try:
        if (
            type(value) is not dict
            or set(value) != {"schema", "target", "library", "minor"}
            or type(value["schema"]) is not int
            or value["schema"] not in {1, 2}
            or type(value["minor"]) is not int
            or not 0 <= value["minor"] < 255
        ):
            raise ValueError
        validate_manifest(value["library"])
        if value["schema"] != value["library"]["schema"]:
            raise ValueError
        target = value["target"]
        if (
            type(target) is not dict
            or set(target) != {"index", "uuid", "model", "driver", "architecture"}
            or type(target["index"]) is not int
            or target["index"] < 0
            or type(target["uuid"]) is not str
            or not GPU_UUID.fullmatch(target["uuid"])
            or type(target["model"]) is not str
            or not 1 <= len(target["model"]) <= 120
            or not target["model"].isascii()
            or type(target["driver"]) is not str
            or not VERSION.fullmatch(target["driver"])
            or target["driver"] != value["library"]["driver_version"]
            or type(target["architecture"]) is not list
            or len(target["architecture"]) != 2
            or any(type(n) is not int or not 0 <= n <= 99 for n in target["architecture"])
            or target["architecture"][0] == 0
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise SandboxUnavailable("Invalid selected NVIDIA observer identity") from None
    return value


def devices(value):
    validate_observer(value)
    return {"/dev/nvidiactl": (195, 255), f"/dev/nvidia{value['minor']}": (195, value["minor"])}


def check_empty_caps(path):
    # NVML 590 creates this private directory while reading metadata. It grants
    # no extra device: permit only an empty, owned directory on the same /dev.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        named = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_dev != path.parent.stat().st_dev
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            or os.listdir(fd)
        ):
            raise SandboxUnavailable("Unexpected NVIDIA capability directory or nodes")
    finally:
        os.close(fd)


def check_devices(value):
    expected = devices(value)
    present = {str(path) for path in Path("/dev").glob("nvidia*")}
    if "/dev/nvidia-caps" in present:
        check_empty_caps(Path("/dev/nvidia-caps"))
        present.remove("/dev/nvidia-caps")
    if present != set(expected):
        raise SandboxUnavailable("Expected only the selected NVIDIA management device nodes")
    for path, numbers in expected.items():
        info = Path(path).lstat()
        if (
            not stat.S_ISCHR(info.st_mode)
            or (os.major(info.st_rdev), os.minor(info.st_rdev)) != numbers
        ):
            raise SandboxUnavailable("Selected NVIDIA device node changed")


def check_prerequisites(value, view):
    # NVML and SMI can both return an empty list in a private PID namespace.
    # Agreement between those restricted sources is not evidence of an idle GPU.
    view.require_same_pid_namespace()
    try:
        verify_bundle(value["library"])
        check_devices(value)
        kernel = read_file(view.fd, "driver/nvidia/version", 4096)
        if version_from_kernel(kernel) != value["target"]["driver"]:
            raise SandboxUnavailable("NVIDIA kernel and management library changed")
    except (OSError, ValueError, UnicodeError):
        raise SandboxUnavailable("NVIDIA observation prerequisites cannot be verified") from None


def pid_set(value):
    if (
        type(value) is not list
        or len(value) > MAX_PIDS
        or any(type(pid) is not int or not 0 < pid < 2**31 for pid in value)
        or len(set(value)) != len(value)
    ):
        raise SandboxUnavailable("Invalid NVIDIA process inventory")
    return set(value)


def validate_observation(value, saved):
    validate_observer(saved)
    full = saved["schema"] == 2
    window = full and type(value) is dict and value.get("schema") == 3
    expected = {
        "schema",
        "bundle",
        "uuid",
        "model",
        "driver",
        "architecture",
        "minor",
        "nvml_version",
        "pids",
    }
    if full:
        expected.add("smi_version")
    if window:
        expected.add("inventory_samples")
    if (
        type(value) is not dict
        or set(value) != expected
        or type(value["schema"]) is not int
        or value["schema"] not in ({2, 3} if full else {1})
        or (full and value["smi_version"] != saved["target"]["driver"])
        or value["bundle"] != manifest_digest(saved["library"])
        or type(value["minor"]) is not int
        or value["minor"] != saved["minor"]
        or any(
            value[key] != saved["target"][key]
            for key in ("uuid", "model", "driver", "architecture")
        )
        or type(value["architecture"]) is not list
        or any(type(n) is not int for n in value["architecture"])
        or type(value["nvml_version"]) is not str
        or not re.fullmatch(r"[0-9.]{1,64}", value["nvml_version"])
        or type(value["pids"]) is not dict
        or set(value["pids"]) != {"compute", "graphics", "mps"} | ({"inventory"} if full else set())
    ):
        raise SandboxUnavailable("NVIDIA management observation identity changed")
    union = set()
    for found in value["pids"].values():
        union.update(pid_set(found))
    if len(union) > MAX_PIDS:
        raise SandboxUnavailable("NVIDIA process inventory exceeded its bound")
    if window:
        samples = value["inventory_samples"]
        if (
            type(samples) is not list
            or len(samples) != 2
            or pid_set(samples[0]) | pid_set(samples[1]) != set(value["pids"]["inventory"])
        ):
            raise SandboxUnavailable("NVIDIA complete inventory samples changed")
    if full and union != set(value["pids"]["inventory"]):
        raise SandboxUnavailable("NVIDIA process sources disagree; device state is unavailable")
    return sorted(union)


class ContainerDeviceMonitor:
    def __init__(self, control):
        self.control = control
        if control.saved.get("version") not in {3, 4} or control.host_processes is None:
            raise SandboxUnavailable("Explicit NVIDIA observation controller required")
        self.saved = copy.deepcopy(validate_observer(control.saved["device_observer"]))
        if control.saved["version"] != self.saved["schema"] + 2:
            raise SandboxUnavailable("NVIDIA observer and controller profiles differ")
        self.owned = self.service = None
        self.check()

    def check(self):
        self.control.check()
        if (
            self.control.saved.get("device_observer") != self.saved
            or self.control.guard.device_uuid != self.saved["target"]["uuid"]
        ):
            raise SandboxUnavailable("NVIDIA observer changed its selected device")

    def read(self, *, on_sample=None):
        """Management metadata only; a successful read does not imply an idle GPU."""
        self.check()
        deadline = time.monotonic() + 3
        if self.saved["schema"] == 2:
            version = utility_version(
                self.smi_query(deadline, "--version", maximum=4096),
                self.saved["target"]["driver"],
            )
            before = self.smi_inventory(deadline)
            if on_sample is not None:
                on_sample(before)
        result = run_command(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("nvml_reader.py")),
                self.saved["target"]["uuid"],
            ],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"},
            deadline=deadline,
            seconds=3,
            on_tick=self.check,
            max_output=256 * 1024,
        )
        value = decode_frame(result.stdout)
        if self.saved["schema"] == 2:
            value = self.full_inventory(
                value, deadline, before=before, version=version, on_sample=on_sample
            )
        validate_observation(value, self.saved)
        self.check()
        if time.monotonic() >= deadline:
            raise SandboxUnavailable("NVIDIA observation deadline expired")
        return value

    def task_device_spec(self):
        if self.saved["schema"] != 2:
            raise SandboxUnavailable("A GPU task requires the complete process source")
        self.check()
        value = {
            "schema": 1,
            "profile": PROFILE,
            "uuid": self.saved["target"]["uuid"],
            "driver": self.saved["target"]["driver"],
            "minor": self.saved["minor"],
            "uvm_major": kernel_uvm_major(
                read_file(self.control.host_processes.fd, "devices", 65536)
            ),
        }
        self.check()
        return validate_selection(value)

    def smi_query(self, deadline, *arguments, maximum):
        result = run_command(
            [str(DIRECTORY / "nvidia-smi"), *arguments],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LD_LIBRARY_PATH": str(DIRECTORY)},
            deadline=deadline,
            seconds=3,
            on_tick=self.check,
            max_output=maximum,
        )
        if result.stderr:
            raise SandboxUnavailable("NVIDIA inventory query returned a diagnostic")
        return result.stdout

    def smi_inventory(self, deadline):
        return inventory(
            self.smi_query(
                deadline,
                "-i",
                self.saved["target"]["uuid"],
                "--query",
                "--xml-format",
                maximum=MAX_XML,
            ),
            self.saved,
        )

    def full_inventory(self, value, deadline, *, before, version, on_sample=None):
        # The native child returns its original schema. Never silently overwrite
        # an existing full inventory, extra field or category from that child.
        if (
            type(value) is not dict
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or "smi_version" in value
            or "inventory_samples" in value
            or type(value.get("pids")) is not dict
            or set(value["pids"]) != {"compute", "graphics", "mps"}
        ):
            raise SandboxUnavailable("Unexpected native NVIDIA observation")
        after = self.smi_inventory(deadline)
        if on_sample is not None:
            on_sample(after)
        pids = sorted(pid_set(before) | pid_set(after))
        return {
            **value,
            "schema": 3,
            "smi_version": version,
            "pids": {**value["pids"], "inventory": pids},
            "inventory_samples": [before, after],
        }

    def observe(self, service=None):
        if self.owned is not None and service is not self.service:
            raise SandboxUnavailable("An observer cannot switch its original task")
        if service is not None and self.owned is None:
            self.owned = OwnedProcesses(self.control, service)
            self.service = service

        def read_inventory():
            return validate_observation(self.read(), self.saved)

        if self.owned is None:
            pids = read_inventory()
            if pids:
                raise SandboxUnavailable("Selected GPU is occupied; nothing was stopped")
        elif self.saved["schema"] == 2:
            pids = self.owned.observe_samples(
                lambda capture: validate_observation(self.read(on_sample=capture), self.saved)
            )
        else:
            pids = self.owned.observe_owned(read_inventory)
        self.check()
        return pids

    def observe_termination(self, service, *, deadline=None):
        """Triggered maintenance only; preserve the anchor and all nonempty checks."""
        if (
            self.saved["schema"] != 2
            or self.owned is None
            or service is not self.service
            or self.owned.record["purpose"] != "maintenance"
        ):
            raise SandboxUnavailable("Termination requires the original full maintenance observer")
        pids = self.owned.observe_samples(
            lambda capture: validate_observation(self.read(on_sample=capture), self.saved),
            termination_deadline=self.owned.termination_deadline(deadline),
        )
        self.check()
        return pids
