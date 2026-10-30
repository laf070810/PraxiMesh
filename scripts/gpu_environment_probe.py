"""Probe one explicitly selected GPU in a disposable rootless gVisor environment.

This runs reviewed infrastructure fixtures only, never learner code, and produces
no judge receipt. See docs/deployment/gpu.md for prerequisites and scope.
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
IMAGE = "nvcr.io/nvidia/k8s/cuda-sample@sha256:6337237230602d2c063263922dc1db6c8ecc8d23c1f8438e172fe912f10f3973"
RELEASE = "release-20261026.0"
ARCHIVE_SHA512 = "26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a"
RUNSC_SHA256 = "7d658f2c3a5602d28c391285eb4ee0740e7dd21d40731324049a29620e51cf5e"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def gpu_selector(value):
    if not re.fullmatch(
        r"(?:[0-9]+|GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", value
    ):
        raise argparse.ArgumentTypeError(
            "Select exactly one GPU index or full UUID; all is forbidden"
        )
    return value


def runtime_from_archive(archive, destination):
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha512").hexdigest()
    if digest != ARCHIVE_SHA512:
        raise ValueError(f"Archive must match the pinned {RELEASE} SHA-512")
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    with (destination / "runsc").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != RUNSC_SHA256 or not (destination / "gvisor-bin").is_dir():
        raise ValueError("Pinned runsc and its sidecar directory are required")
    return digest


def selected_cdi(spec, target):
    spec = deepcopy(spec)
    spec["devices"] = [d for d in spec["devices"] if d["name"] == target["uuid"]]
    if len(spec["devices"]) != 1:
        raise ValueError("CDI must contain exactly one matching GPU UUID")
    gpu_node = f"/dev/nvidia{target['index']}"
    allowed = {gpu_node, "/dev/nvidiactl", "/dev/nvidia-uvm"}
    selected = set()
    for edits in [spec["containerEdits"], spec["devices"][0]["containerEdits"]]:
        edits["deviceNodes"] = [d for d in edits.get("deviceNodes", []) if d["path"] in allowed]
        selected.update(d["path"] for d in edits["deviceNodes"])
        for hook in edits.get("hooks", []):
            hook["env"] = [
                value for value in hook.get("env", []) if not value.startswith("GOMAXPROCS=")
            ] + ["GOMAXPROCS=2"]
    if selected != allowed:
        raise ValueError("CDI is missing a required GPU device node")
    return spec


def child(base, out, name):
    target = json.loads((base / "target.json").read_text())
    configuration = json.loads((base / "probe.json").read_text())
    kernel_probe = None
    image = IMAGE
    if configuration["mode"] in {"kernels", "faults", "sessions"}:
        from scripts import gpu_kernel_probe as kernel_probe

        image = kernel_probe.IMAGE
    if configuration.get("image_id"):
        image = configuration["image_id"]
    report = {
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "base": str(base),
        "unit": name,
        "target": target,
        "mode": configuration["mode"],
        "steps": [],
        "passed": False,
    }
    containers = []
    env = dict(os.environ)
    for key in ["CONTAINER_HOST", "CONTAINER_CONNECTION", "DOCKER_HOST", "DOCKER_CONTEXT"]:
        env.pop(key, None)
    env["CONTAINERS_CONF"] = str(base / "containers.conf")
    env["GOMAXPROCS"] = "2"

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    def command(args, timeout=30, check=True, label=None, stdin=None):
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, env=env, timeout=timeout, input=stdin
            )
        except subprocess.TimeoutExpired as error:
            report["steps"].append(
                {
                    "label": label or args[0],
                    "timed_out": True,
                    "stdout": (error.stdout or b"").decode(errors="replace")[-16000:],
                    "stderr": (error.stderr or b"").decode(errors="replace")[-6000:],
                }
            )
            save()
            raise
        report["steps"].append(
            {
                "label": label or args[0],
                "exit_code": result.returncode,
                "stdout": result.stdout[-16000:],
                "stderr": result.stderr[-6000:],
            }
        )
        save()
        if check and result.returncode:
            raise RuntimeError(f"{label or args[0]} failed: {result.stderr[-2000:]}")
        return result

    def podman_arguments(*args):
        diagnostics = (
            ["--runtime-flag=debug=false"]
            if configuration["mode"] == "sessions"
            else [
                "--runtime-flag=debug=true",
                "--runtime-flag=debug-log=" + str(out / "runsc") + "/",
            ]
        )
        return [
            "podman",
            "--root",
            str(base / "data"),
            "--runroot",
            str(base / "run"),
            "--tmpdir",
            str(base / "tmp"),
            "--storage-driver=overlay",
            "--storage-opt=overlay.mount_program=/usr/bin/fuse-overlayfs",
            "--cgroup-manager=cgroupfs",
            "--events-backend=file",
            "--runtime",
            str(base / "runtime/runsc"),
            "--runtime-flag=nvproxy=true",
            "--runtime-flag=network=none",
            *diagnostics,
            *args,
        ]

    def podman(*args, timeout=30, check=True, stdin=None):
        return command(
            podman_arguments(*args),
            timeout=timeout,
            check=check,
            stdin=stdin,
            label="podman " + args[0],
        )

    def container_arguments(container, entrypoint, args, *, interactive=False, diagnostics=True):
        options = [
            "--pull=never",
            "--name",
            container,
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
            "--device=nvidia.com/gpu=" + target["uuid"],
        ]
        if entrypoint:
            options.append("--entrypoint=" + entrypoint)
        if interactive:
            options.append("--interactive")
        if kernel_probe is not None:
            options += kernel_probe.container_options(base, diagnostics=diagnostics)
        # CDI already selects exactly one UUID. Do not inherit the image's
        # NVIDIA_VISIBLE_DEVICES=all and trigger legacy reinjection in runsc.
        options += [
            "--env=NVIDIA_VISIBLE_DEVICES=void",
            "--env=NVIDIA_DRIVER_CAPABILITIES=compute,utility",
        ]
        return [*options, image, *args]

    def device_idle(label):
        for attempt in range(10):
            active = command(
                [
                    "nvidia-smi",
                    "-i",
                    target["uuid"],
                    "--query-compute-apps=pid",
                    "--format=csv,noheader",
                ],
                timeout=10,
                label="device idle after " + label,
            )
            if not active.stdout.strip():
                return
            if attempt == 9:
                raise RuntimeError("Target still has a compute task; device not reusable")
            time.sleep(0.2)

    def session(label, *, mode, on_tick):
        from judge.gpu_session import GPUSession

        container = name + "-" + label
        containers.append(container)
        podman(
            "create",
            *container_arguments(
                container,
                "python",
                ["-B", "-m", "judge.gpu_runtime"],
                interactive=True,
                diagnostics=False,
            ),
        )
        details = json.loads(podman("inspect", container).stdout)[0]
        host = details["HostConfig"]
        require(
            details.get("OCIRuntime") == str(base / "runtime/runsc")
            and host["ReadonlyRootfs"]
            and not host["Privileged"]
            and host["NetworkMode"] == "none",
            "Session isolation configuration changed before execution",
        )

        def oom_kills():
            events = dict(
                line.split()
                for line in Path("/sys/fs/cgroup/workloads/memory.events").read_text().splitlines()
            )
            return int(events["oom_kill"])

        initial_ooms = oom_kills()

        def exit_verdict(_code):
            details = json.loads(podman("inspect", container).stdout)[0]
            return (
                "memory_limit"
                if not details["State"]["Running"] and oom_kills() > initial_ooms
                else None
            )

        def cleanup():
            before = json.loads(podman("inspect", container).stdout)[0]
            if before["State"]["Running"]:
                podman("kill", "--signal=KILL", container, timeout=15)
            podman("wait", container, timeout=15)
            after = json.loads(podman("inspect", container).stdout)[0]
            require(not after["State"]["Running"], "Session workload has not stopped")
            device_idle(label)
            report.setdefault("session_cleanup", []).append(
                {
                    "name": label,
                    "running_before_cleanup": before["State"]["Running"],
                    "exit_code": after["State"]["ExitCode"],
                    "device_idle": True,
                }
            )
            save()

        process = subprocess.Popen(
            podman_arguments("start", "--attach", "--interactive", container),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return GPUSession(
            process,
            cleanup,
            architecture=target["architecture"],
            mode=mode,
            on_tick=on_tick,
            exit_verdict=exit_verdict,
        )

    def run(
        label,
        entrypoint=None,
        args=(),
        *,
        stdin=None,
        expected_exit=0,
        timeout=60,
        expected_timeout=False,
    ):
        container = name + "-" + label
        containers.append(container)
        options = container_arguments(container, entrypoint, args, interactive=stdin is not None)
        timed_out = False
        started = time.monotonic()
        try:
            result = podman("run", *options, timeout=timeout, check=False, stdin=stdin)
        except subprocess.TimeoutExpired as error:
            if not expected_timeout:
                raise
            timed_out = True
            # The CLI process timing out does not mean the workload stopped.
            # Kill only this owned container, then wait for confirmed termination.
            podman("kill", "--signal=KILL", container, timeout=15)
            stopped = podman("wait", container, timeout=15)
            require(stopped.stdout.strip() == "137", "Timed-out workload did not terminate")
            result = subprocess.CompletedProcess([], 137, (error.stdout or b"").decode(), "")
            report[label + "_termination"] = {
                "deadline_seconds": timeout,
                "elapsed_seconds": time.monotonic() - started,
                "exit_code": 137,
            }
        require(timed_out == expected_timeout, f"{label}: timeout expectation mismatch")
        require(
            result.returncode == expected_exit,
            f"{label}: expected exit {expected_exit}, got {result.returncode}",
        )
        details = json.loads(podman("inspect", container).stdout)[0]
        require(not details["State"]["Running"], "Workload still running after result")
        require(details.get("OCIRuntime") == str(base / "runtime/runsc"), "Unexpected OCI runtime")
        host = details["HostConfig"]
        require(
            host["ReadonlyRootfs"] and not host["Privileged"] and host["NetworkMode"] == "none",
            "Container isolation configuration changed",
        )
        report[label + "_inspection"] = {
            key: details.get(key) for key in ["Id", "Image", "OCIRuntime", "State", "HostConfig"]
        }
        if configuration["mode"] == "faults":
            # Host-side NVML observation in the trusted private controller;
            # never terminate an unknown process or reset the card.
            device_idle(label)
            report[label + "_device_idle"] = True
        save()
        return result.stdout.strip()

    try:
        (out / "runsc").mkdir(mode=0o700)
        require(
            Path("/proc/self/cgroup").read_text().strip() == "0::/manager",
            "Expected private cgroup namespace",
        )
        report["self_cgroup"] = Path("/proc/self/cgroup").read_text().strip()
        cg = Path("/sys/fs/cgroup")
        controllers = (cg / "cgroup.controllers").read_text().split()
        require({"cpu", "memory", "pids"} <= set(controllers), "Missing delegated controllers")
        (cg / "cgroup.subtree_control").write_text(" ".join("+" + c for c in controllers))
        report["controllers"] = controllers
        for directory in ["data", "run", "tmp"]:
            (base / directory).mkdir(mode=0o700)
        info = json.loads(podman("info", "--format=json").stdout)
        require(info["store"]["graphRoot"] == str(base / "data"), "Unexpected Podman store")
        require(
            info["host"]["cgroupVersion"] == "v2" and info["host"]["cgroupManager"] == "cgroupfs",
            "Expected cgroup v2 with cgroupfs",
        )
        report["store"] = info["store"]["graphRoot"]
        workloads = cg / "workloads"
        workloads.mkdir()
        (workloads / "cgroup.subtree_control").write_text(" ".join("+" + c for c in controllers))
        limits = {
            "cpu.max": "50000 100000",
            "memory.max": "536870912",
            "memory.swap.max": "0",
            "pids.max": "256",
        }
        for key, value in limits.items():
            (workloads / key).write_text(value)
            require((workloads / key).read_text().strip() == value, f"Failed to enforce {key}")
        report["workload_parent_limits"] = limits
        print(
            json.dumps({"phase": "private_namespace_ready", "result": str(out / "result.json")}),
            flush=True,
        )
        if configuration.get("image_id"):
            podman("load", "--input=" + str(base / "image.oci.tar"), timeout=90)
            loaded = json.loads(podman("image", "inspect", image).stdout)[0]
            require(
                loaded["Id"].removeprefix("sha256:") == image.removeprefix("sha256:"),
                "Imported image identity mismatch",
            )
        else:
            # Fixed public images must not inherit stale/private registry credentials.
            authfile = base / "registry-auth.json"
            authfile.write_text('{"auths": {}}\n')
            authfile.chmod(0o600)
            podman(
                "pull",
                "--authfile=" + str(authfile),
                "--retry=3",
                "--retry-delay=2s",
                image,
                timeout=240,
            )
        report["sample_image"] = image
        print(json.dumps({"phase": "sample_image_ready"}), flush=True)
        visible = run(
            "identity",
            "nvidia-smi",
            ["--query-gpu=uuid,name,driver_version", "--format=csv,noheader"],
        )
        report["visible_devices"] = visible
        require(
            len(visible.splitlines()) == 1 and target["uuid"] in visible,
            "Unexpected visible GPU set",
        )
        print(json.dumps({"phase": "gpu_identity_passed", "visible_devices": visible}), flush=True)
        if configuration["mode"] == "sessions":
            from scripts.gpu_session_probe import verify_sessions

            verify_sessions(session, report, save)
        elif kernel_probe is not None:
            kernel_probe.verify_fixtures(
                run, report, save, faults=configuration["mode"] == "faults"
            )
        else:
            output = run("vectoradd")
            report["vectoradd_output"] = output
            require("Test PASSED" in output, "Vendor CUDA correctness check failed")
            print(json.dumps({"phase": "vectoradd_passed", "output": output}), flush=True)

        def cpu_stats():
            return {
                key: int(value)
                for key, value in (
                    line.split() for line in (workloads / "cpu.stat").read_text().splitlines()
                )
            }

        before = cpu_stats()
        started = time.monotonic()
        run(
            "cpu-quota",
            "/bin/sh",
            [
                "-c",
                "timeout 8 /bin/sh -c 'while :; do :; done'; result=$?; test \"$result\" -eq 124",
            ],
        )
        elapsed = time.monotonic() - started
        after = cpu_stats()
        used = (after["usage_usec"] - before["usage_usec"]) / 1e6
        throttled = after["nr_throttled"] - before["nr_throttled"]
        report["cpu_quota_probe"] = {
            "elapsed_seconds": elapsed,
            "cpu_seconds": used,
            "ratio": used / elapsed,
            "throttled_periods": throttled,
        }
        require(
            elapsed >= 8 and used >= 3 and used / elapsed <= 0.6 and throttled > 0,
            "Actual CPU quota/throttling check failed",
        )
        report["workload_parent_after"] = {
            key: (workloads / key).read_text().strip()
            for key in [*limits, "cpu.stat", "memory.events", "pids.events", "cgroup.procs"]
        }
        for key, value in limits.items():
            require(report["workload_parent_after"][key] == value, f"Workload changed {key}")
        report["passed"] = True
    except BaseException as error:
        report["error"] = str(error)
        report["resource_state"] = {}
        for file in Path("/sys/fs/cgroup").rglob("pids.events"):
            if len(report["resource_state"]) >= 20:
                break
            group = file.parent
            report["resource_state"][str(group)] = {
                key: (group / key).read_text().strip()
                for key in [
                    "pids.current",
                    "pids.max",
                    "pids.events",
                    "cpu.max",
                    "cpu.stat",
                    "memory.max",
                    "memory.events",
                ]
                if (group / key).exists()
            }
        print(
            json.dumps(
                {"phase": "failed", "error": str(error), "result": str(out / "result.json")}
            ),
            flush=True,
        )
    finally:
        for container in containers:
            try:
                podman("rm", "--force", container, timeout=20, check=False)
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(str(error))
        if report.get("store"):
            try:
                info = json.loads(podman("info", "--format=json").stdout)
                require(
                    info["store"]["graphRoot"] == str(base / "data"),
                    "Refusing cleanup of unexpected Podman store",
                )
                podman("image", "prune", "--all", "--force", timeout=45)
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(str(error))
        overlay = base / "data/overlay"
        if os.path.ismount(overlay):
            try:
                command(["umount", str(overlay)], label="unmount private overlay")
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(str(error))
        for directory in ["data", "run", "tmp"]:
            try:
                if (base / directory).exists():
                    shutil.rmtree(base / directory)
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(str(error))
        report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        save()
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "cleanup_errors": report.get("cleanup_errors", []),
                    "result": str(out / "result.json"),
                }
            ),
            flush=True,
        )
    return 0 if report["passed"] and not report.get("cleanup_errors") else 1


def parent(options):
    if options.fault_probes and not options.nvrtc_archive:
        raise ValueError("--fault-probes requires the pinned --nvrtc-archive")
    if options.image_archive and not options.nvrtc_archive:
        raise ValueError("--image-archive requires the pinned --nvrtc-archive")
    sessions = getattr(options, "session_probes", False)
    if sessions and (not options.nvrtc_archive or options.fault_probes):
        raise ValueError("--session-probes requires NVRTC and a separate run from fault probes")
    if os.getuid() == 0:
        raise ValueError("Run as the delegated non-root user, not with sudo")
    check = subprocess.run(
        [
            "nvidia-smi",
            "-i",
            options.gpu,
            "--query-gpu=index,uuid,name,driver_version,compute_cap",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = list(csv.reader(check.stdout.splitlines(), skipinitialspace=True))
    if len(rows) != 1 or len(rows[0]) != 5:
        raise ValueError("Expected exactly one GPU from nvidia-smi")
    index, device_uuid, model, driver, architecture = rows[0]
    require(re.fullmatch(r"[1-9][0-9]*\.[0-9]+", architecture), "Invalid device architecture")
    target = {
        "index": int(index),
        "uuid": device_uuid,
        "model": model,
        "driver": driver,
        "architecture": list(map(int, architecture.split("."))),
    }
    from judge.gpu_device_lock import DeviceLock

    locks = getattr(options, "lock_directory", None) or (
        Path("/run/user") / str(os.getuid()) / "praximesh-gpu-locks"
    )
    with DeviceLock(locks, target["uuid"]) as guard:
        return owned_parent(options, target, guard)


def owned_parent(options, target, guard):
    sessions = getattr(options, "session_probes", False)
    driver = target["driver"]
    guard.assert_held()
    check = subprocess.run(
        ["nvidia-smi", "-i", target["uuid"], "--query-compute-apps=pid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    require(not check.stdout.strip(), "Target has an existing compute task; nothing was stopped")
    name = "px-gpu-cgns-" + uuid.uuid4().hex[:12]
    base = Path(tempfile.mkdtemp(prefix=name + "-"))
    base.chmod(0o755)
    out = ROOT / "test-results" / name
    out.mkdir(mode=0o700, parents=True)
    outer = {
        "unit": name,
        "base": str(base),
        "release": RELEASE,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    code = 1
    proc = None
    try:
        digest = runtime_from_archive(options.runtime_archive, base / "runtime")
        outer["runsc_sha256"] = digest
        mode = (
            "sessions"
            if sessions
            else "faults"
            if options.fault_probes
            else "kernels"
            if options.nvrtc_archive
            else "vendor"
        )
        if options.nvrtc_archive:
            from scripts.gpu_kernel_probe import prepare_artifacts

            outer.update(prepare_artifacts(base, options.nvrtc_archive, ROOT))
        outer["mode"] = mode
        configuration = {"mode": mode}
        if options.image_archive:
            from scripts.gpu_kernel_probe import verify_image_archive

            shutil.copyfile(options.image_archive, base / "image.oci.tar")
            identity = verify_image_archive(base / "image.oci.tar")
            outer.update(identity)
            configuration["image_id"] = identity["image_id"]
        (base / "probe.json").write_text(json.dumps(configuration))
        supported = subprocess.run(
            [str(base / "runtime/runsc"), "nvproxy", "list-supported-drivers"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.splitlines()
        if driver not in [value.strip() for value in supported]:
            raise ValueError(f"Host driver {driver} is not in the pinned runsc supported list")
        (base / "target.json").write_text(json.dumps(target))
        (base / "cdi").mkdir(mode=0o700)
        result = subprocess.run(
            ["nvidia-ctk", "cdi", "generate", "--format=json", "--device-name-strategy=uuid"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        spec = selected_cdi(json.loads(result.stdout), target)
        (base / "cdi/nvidia.json").write_text(json.dumps(spec))
        (base / "containers.conf").write_text(
            "[engine]\ncdi_spec_dirs = [" + json.dumps(str(base / "cdi")) + "]\n"
        )
        args = [
            "systemd-run",
            "--user",
            "--unit=" + name,
            "--collect",
            "--wait",
            "--pipe",
            "--property=Delegate=yes",
            "--property=CPUQuota=200%",
            "--property=MemoryMax=2G",
            "--property=TasksMax=512",
            "--property=RuntimeMaxSec=360",
        ]
        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ]:
            if key in os.environ:
                args.append("--setenv=" + key + "=" + os.environ[key])
        args += [
            "rootlesskit",
            "--state-dir=" + str(base / "state"),
            "--pidns",
            "--cgroupns",
            "--evacuate-cgroup2=manager",
            "--net=slirp4netns",
            "--disable-host-loopback",
            "--copy-up=/etc",
            "--copy-up=/run",
            "--copy-up=/run/lock",
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            str(base),
            str(out),
            name,
        ]
        with (out / "service.log").open("w") as log:
            guard.assert_held()
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            outer["process_id"] = proc.pid
            (out / "outer.json").write_text(json.dumps(outer, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "phase": "started",
                        "unit": name,
                        "result": str(out / "result.json"),
                        "log": str(out / "service.log"),
                    }
                ),
                flush=True,
            )
            try:
                code = proc.wait(timeout=390)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["systemctl", "--user", "stop", name + ".service"],
                    capture_output=True,
                    timeout=20,
                )
                proc.wait(timeout=30)
                raise
            outer["exit_code"] = code
    except BaseException as error:
        outer["error"] = str(error)
        code = 1
    finally:
        stop = subprocess.run(
            ["systemctl", "--user", "stop", name + ".service"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        outer["stop_exit"] = stop.returncode
        if proc is not None:
            proc.wait(timeout=30)
        time.sleep(0.3)
        cg = (
            Path("/sys/fs/cgroup")
            / f"user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{name}.service"
        )
        outer["temporary_cgroup_removed"] = not cg.exists()
        if cg.exists():
            outer.setdefault("cleanup_errors", []).append("Temporary cgroup still exists")
        try:
            shutil.rmtree(base)
            outer["temporary_directory_removed"] = not base.exists()
        except Exception as error:
            outer.setdefault("cleanup_errors", []).append(str(error))
        outer["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        (out / "outer.json").write_text(json.dumps(outer, indent=2) + "\n")
        print(json.dumps(outer), flush=True)
    return 1 if outer.get("cleanup_errors") else code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpu", required=True, type=gpu_selector, help="One authorized, idle GPU index or UUID"
    )
    parser.add_argument(
        "--lock-directory",
        type=Path,
        help="Shared private host lock directory; defaults to /run/user/UID/praximesh-gpu-locks",
    )
    parser.add_argument(
        "--runtime-archive",
        type=Path,
        required=True,
        help=f"Unmodified official {RELEASE} gvisor.tar.bz2",
    )
    parser.add_argument(
        "--nvrtc-archive",
        type=Path,
        help="Optional pinned NVIDIA NVRTC 13.1.115 archive for CUDA kernel fixtures",
    )
    parser.add_argument(
        "--fault-probes",
        action="store_true",
        help="With NVRTC, run reviewed GPU faults and recovery on the dedicated card",
    )
    parser.add_argument(
        "--session-probes",
        action="store_true",
        help="With NVRTC, verify interactive grading, timing, cancellation and owned cleanup",
    )
    parser.add_argument(
        "--image-archive",
        type=Path,
        help="Optional OCI archive of the same pinned Python image, verified before import",
    )
    options = parser.parse_args()
    try:
        return parent(options)
    except Exception as error:
        print(f"GPU environment probe failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(child(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
    raise SystemExit(main())
