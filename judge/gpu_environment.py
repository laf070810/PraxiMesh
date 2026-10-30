"""Private offline Podman/runsc runtime for trusted controller entry points.

Must run inside an owned RootlessKit task service. CUDA requires explicit device
and compiler preparation; the CPU infrastructure check is never a CUDA fallback.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from judge import gpu_artifacts
from judge.gpu_command import run_command
from judge.gpu_device_lock import GPU_UUID
from judge.gpu_namespace import prepare_workload_cgroup
from judge.gpu_recovery import record_runtime_owner
from judge.gpu_session import GPUSession
from judge.sandbox import SandboxUnavailable

ARCHIVE_BYTES = 256 * 1024 * 1024
LABEL = re.compile(r"[a-z][a-z0-9-]{0,30}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def copy_archive(source, destination, on_tick):
    """Copy a bounded regular archive before verifying the private snapshot."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as incoming:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= ARCHIVE_BYTES:
            raise SandboxUnavailable("Private runtime archive has an invalid size or type")
        with Path(destination).open("xb") as outgoing:
            size = 0
            while chunk := incoming.read(1024 * 1024):
                on_tick()
                size += len(chunk)
                if size > ARCHIVE_BYTES:
                    raise SandboxUnavailable("Private runtime archive grew beyond its budget")
                outgoing.write(chunk)
        if size != info.st_size:
            raise SandboxUnavailable("Private runtime archive changed during staging")


class PrivateRuntime:
    def __init__(self, base, *, deadline, on_tick):
        self.base = Path(base)
        if not self.base.is_absolute() or self.base.is_symlink():
            raise ValueError("An absolute owned workspace is required")
        self.deadline, self.on_tick = deadline, on_tick
        self.limits = prepare_workload_cgroup()
        self.identity = None
        self.target = None
        self.containers = {}
        self.store_started = False
        self.closed = False
        record_runtime_owner(self.base)
        for name in ("data", "run", "tmp", "hooks", "cdi"):
            (self.base / name).mkdir(mode=0o700)
        configuration = self.base / "containers.conf"
        configuration.write_text(
            "[engine]\ncdi_spec_dirs = [" + json.dumps(str(self.base / "cdi")) + "]\n"
        )
        auth = self.base / "registry-auth.json"
        auth.write_text('{"auths": {}}\n')
        self.env = dict(
            os.environ,
            CONTAINERS_CONF=str(configuration),
            REGISTRY_AUTH_FILE=str(auth),
            GOMAXPROCS="2",
        )

    def tick(self):
        if time.monotonic() >= self.deadline:
            raise SandboxUnavailable("Private runtime reservation expired")
        self.on_tick()
        if time.monotonic() >= self.deadline:
            raise SandboxUnavailable("Private runtime reservation expired")

    def arguments(self, *args):
        return [
            "/usr/bin/podman",
            "--remote=false",
            "--root=" + str(self.base / "data"),
            "--runroot=" + str(self.base / "run"),
            "--tmpdir=" + str(self.base / "tmp"),
            "--storage-driver=overlay",
            "--storage-opt=overlay.mount_program=/usr/bin/fuse-overlayfs",
            "--cgroup-manager=cgroupfs",
            "--events-backend=none",
            "--hooks-dir=" + str(self.base / "hooks"),
            "--runtime=" + str(self.base / "runtime/runsc"),
            "--runtime-flag=network=none",
            "--runtime-flag=debug=false",
            "--runtime-flag=nvproxy=" + ("true" if self.target else "false"),
            *args,
        ]

    def command(self, *args, cleanup=False, seconds=10, **options):
        return run_command(
            self.arguments(*args),
            env=self.env,
            deadline=self.deadline,
            on_tick=(lambda: None) if cleanup else self.tick,
            seconds=seconds,
            **options,
        )

    def inspect_json(self, *args, cleanup=False):
        try:
            return json.loads(self.command(*args, cleanup=cleanup).stdout)
        except (ValueError, UnicodeError):
            raise SandboxUnavailable("Private runtime inspection is invalid") from None

    def check_store(self, *, cleanup=False):
        info = self.inspect_json("info", "--format=json", cleanup=cleanup)
        if (
            info["store"]["graphRoot"] != str(self.base / "data")
            or info["store"]["runRoot"] != str(self.base / "run")
            or info["host"]["cgroupVersion"] != "v2"
            or info["host"]["cgroupManager"] != "cgroupfs"
        ):
            raise SandboxUnavailable("Private runtime store or cgroup configuration changed")
        return info

    def prepare(self, runtime_archive, image_archive):
        if self.identity is not None:
            raise SandboxUnavailable("Private runtime was already prepared")
        self.tick()
        runtime = self.base / "runtime.tar.bz2"
        image = self.base / "image.oci.tar"
        copy_archive(runtime_archive, runtime, self.tick)
        digest = gpu_artifacts.runtime_from_archive(runtime, self.base / "runtime")
        self.tick()
        copy_archive(image_archive, image, self.tick)
        identity = gpu_artifacts.verify_image_archive(image)
        self.tick()
        self.store_started = True  # Include a partially initialized store in cleanup.
        info = self.check_store()
        self.command("load", "--input=" + str(image), seconds=45)
        loaded = self.inspect_json("image", "inspect", identity["image_id"])[0]
        if loaded["Id"].removeprefix("sha256:") != identity["image_id"].removeprefix("sha256:"):
            raise SandboxUnavailable("Private runtime imported an unexpected image")
        self.identity = dict(
            identity,
            runsc_sha256=digest,
            runsc_release=gpu_artifacts.RELEASE,
            podman_version=info["version"]["Version"],
        )
        return dict(self.identity)

    def prepare_gpu(self, target, nvrtc_archive):
        if self.identity is None or self.target is not None:
            raise SandboxUnavailable("Prepare the offline runtime before the single GPU")
        if (
            type(target) is not dict
            or not GPU_UUID.fullmatch(target.get("uuid", ""))
            or type(target.get("index")) is not int
            or target["index"] < 0
            or type(target.get("driver")) is not str
            or type(target.get("architecture")) is not list
            or len(target["architecture"]) != 2
            or any(type(value) is not int or value < 0 for value in target["architecture"])
        ):
            raise ValueError("An explicitly verified single GPU identity is required")
        compiler = self.base / "nvrtc.tar.xz"
        copy_archive(nvrtc_archive, compiler, self.tick)
        identity = gpu_artifacts.prepare_kernel_artifacts(
            self.base, compiler, Path(__file__).resolve().parents[1]
        )
        supported = (
            run_command(
                [str(self.base / "runtime/runsc"), "nvproxy", "list-supported-drivers"],
                env=self.env,
                deadline=self.deadline,
                on_tick=self.tick,
            )
            .stdout.decode()
            .splitlines()
        )
        if target["driver"] not in [value.strip() for value in supported]:
            raise SandboxUnavailable("Pinned runsc does not support the selected GPU driver")
        generated = run_command(
            [
                "/usr/bin/nvidia-ctk",
                "cdi",
                "generate",
                "--format=json",
                "--device-name-strategy=uuid",
            ],
            env=self.env,
            deadline=self.deadline,
            on_tick=self.tick,
        )
        spec = gpu_artifacts.selected_cdi(json.loads(generated.stdout), target)
        (self.base / "cdi/nvidia.json").write_text(json.dumps(spec))
        self.target = dict(target)
        self.identity.update(identity)
        return dict(self.identity)

    def create(self, label, entrypoint, arguments, *, mounts=()):
        """Only fixed platform programs are supplied here, never learner argv."""
        if self.identity is None or not LABEL.fullmatch(label) or label in self.containers:
            raise ValueError("An unused fixed container label and prepared runtime are required")
        name = self.base.name + "-" + label
        options = [
            "--pull=never",
            "--name=" + name,
            "--network=none",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "--cpus=0.5",
            "--memory=512m",
            "--memory-swap=512m",
            "--pids-limit=256",
            "--cgroup-parent=/workloads",
            "--ipc=none",
            "--log-driver=none",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            "--hostname=praximesh",
            "--http-proxy=false",
            "--no-hosts",
            "--unsetenv-all",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=NVIDIA_VISIBLE_DEVICES=void",
            "--interactive",
            "--entrypoint=" + entrypoint,
        ]
        for source, destination in mounts:
            source = Path(source)
            if not source.resolve().is_relative_to(self.base.resolve()) or ":" in str(source):
                raise ValueError("Only trusted artifacts in this workspace may be mounted")
            options.append("--volume=" + str(source) + ":" + destination + ":ro")
        if self.target:
            options += [
                "--device=nvidia.com/gpu=" + self.target["uuid"],
                "--env=NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            ]
            options += gpu_artifacts.container_options(self.base, diagnostics=False)
        # Track the deterministic owned name before create, even if its CLI dies.
        self.containers[label] = {"name": name, "id": None}
        created = self.command("create", *options, self.identity["image_id"], *arguments)
        container_id = created.stdout.decode().strip()
        if not CONTAINER_ID.fullmatch(container_id):
            raise SandboxUnavailable("Private container creation returned an invalid identity")
        self.containers[label]["id"] = container_id
        self.inspect(label)
        return container_id

    def inspect(self, label, *, cleanup=False):
        owned = self.containers[label]
        details = self.inspect_json("inspect", owned["id"] or owned["name"], cleanup=cleanup)[0]
        host = details["HostConfig"]
        if (
            details["Name"] != owned["name"]
            or (owned["id"] is not None and details["Id"] != owned["id"])
            or details["Image"].removeprefix("sha256:")
            != self.identity["image_id"].removeprefix("sha256:")
            or details["OCIRuntime"] != str(self.base / "runtime/runsc")
            or details["Config"]["User"] != "65534:65534"
            or not host["ReadonlyRootfs"]
            or host["Privileged"]
            or host["NetworkMode"] != "none"
            or host["IpcMode"] != "none"
            or host["Memory"] != 512 * 1024**2
            or host["MemorySwap"] != 512 * 1024**2
            or host["PidsLimit"] != 256
            or host["CpuQuota"] != 50000
            or host["CpuPeriod"] != 100000
            or "no-new-privileges" not in host["SecurityOpt"]
            or host["LogConfig"]["Type"] != "none"
        ):
            raise SandboxUnavailable(
                "Private container identity or isolation configuration changed"
            )
        return details

    def attach(self, label):
        self.tick()
        details = self.inspect(label)
        if details["State"]["Running"] or details["State"]["Status"] != "created":
            raise SandboxUnavailable("Only a new owned container may be started")
        return subprocess.Popen(
            self.arguments("start", "--attach", "--interactive", details["Id"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def stop_container(self, label):
        before = self.inspect(label, cleanup=True)
        if before["State"]["Running"]:
            self.command("kill", "--signal=KILL", before["Id"], cleanup=True, seconds=5)
        if before["State"]["Status"] != "created":
            self.command("wait", before["Id"], cleanup=True, seconds=5)
        after = self.inspect(label, cleanup=True)
        if after["State"]["Running"]:
            raise SandboxUnavailable("Owned container is still running")
        return {
            "id": after["Id"],
            "running_before_cleanup": before["State"]["Running"],
            "exit_code": after["State"]["ExitCode"],
            "stopped": True,
        }

    def kernel_session(self, label, *, mode):
        if self.target is None:
            raise SandboxUnavailable("CUDA sessions require explicit GPU and compiler preparation")
        self.create(label, "/usr/local/bin/python", ["-B", "-m", "judge.gpu_runtime"])

        def oom_kills():
            events = dict(
                line.split()
                for line in Path("/sys/fs/cgroup/workloads/memory.events").read_text().splitlines()
            )
            return int(events["oom_kill"])

        initial_ooms = oom_kills()

        def exit_verdict(_code):
            stopped = not self.inspect(label)["State"]["Running"]
            return "memory_limit" if stopped and oom_kills() > initial_ooms else None

        return GPUSession(
            self.attach(label),
            lambda: self.stop_container(label),
            architecture=self.target["architecture"],
            mode=mode,
            on_tick=self.tick,
            exit_verdict=exit_verdict,
        )

    def close(self):
        if self.closed:
            return
        if self.store_started:
            self.check_store(cleanup=True)
            known = {item["name"] for item in self.containers.values()}
            present = self.inspect_json("ps", "--all", "--format=json", cleanup=True)
            if any(set(item["Names"]) - known for item in present):
                raise SandboxUnavailable("Refusing cleanup of unexpected containers")
            for label, owned in self.containers.items():
                exists = self.command(
                    "container", "exists", owned["id"] or owned["name"], cleanup=True, check=False
                )
                if exists.returncode == 1:
                    continue
                if exists.returncode != 0:
                    raise SandboxUnavailable("Cannot determine owned container state")
                stopped = self.stop_container(label)
                self.command("rm", stopped["id"], cleanup=True, seconds=5)
            if self.inspect_json("ps", "--all", "--format=json", cleanup=True):
                raise SandboxUnavailable("Private store still contains containers")
            self.command("image", "prune", "--all", "--force", cleanup=True, seconds=15)
        overlay = self.base / "data/overlay"
        if os.path.ismount(overlay):
            run_command(
                ["/usr/bin/umount", str(overlay)],
                env=self.env,
                deadline=self.deadline,
                on_tick=lambda: None,
                seconds=5,
            )
        for name in ("data", "run", "tmp"):
            shutil.rmtree(self.base / name)
        self.closed = True
