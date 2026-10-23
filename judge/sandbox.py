"""gVisor code-execution backend. No host-Python or ordinary-Docker fallback.

The trusted controller has Docker access; guests have none. The wrapper is a
guest program, NOT a security mechanism. Security comes from runsc and enforced
container policy. Expected values are never passed to the guest.
"""

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

POLICY_VERSION = "gvisor-python-function-v1"
MEMORY = 128 * 1024 * 1024
OUTPUT = 32 * 1024
WALL_SECONDS = 8
GUEST = r"""
import contextlib, json, sys
request = json.load(sys.stdin)
try:
    code = compile(request["source"], "solution.py", "exec")
except SyntaxError:
    sys.exit(3)
try:
    namespace = {"__name__": "solution"}
    with contextlib.redirect_stdout(sys.stderr):
        exec(code, namespace)
        value = namespace[request["function"]](*request["args"])
    sys.stdout.write(json.dumps({"value": value}, allow_nan=False))
except MemoryError:
    sys.exit(4)
except BaseException:
    sys.exit(2)
"""


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class Execution:
    verdict: str
    value: object = None
    elapsed_ms: int = 0


def policy_digest(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    fingerprint = hashlib.sha256()
    for relative in (
        "judge/sandbox.py",
        "judge/backends.py",
        "judge/wasi.py",
        "judge/wasi_policy.py",
        "judge/wasi_runner.py",
        "judge/wasi_probes.py",
        "judge/javascript.py",
        "judge/stdio.py",
        "judge/stdio_probes.py",
        "judge/javascript_bridge.c",
        "judge/javascript_probes.py",
        "vendor/quickjs/wasi.patch",
        "judge/evaluator.py",
        "judge/agent.py",
        "judge/agent_driver.py",
        "judge/agent_runtime.py",
        "judge/agent_policy.py",
        "judge/agent_probes.py",
        "judge/specialist_probes.py",
        "judge/ml.py",
        "judge/scientific.py",
        "judge/results.py",
        "core/queue.py",
        "core/resources.py",
        "core/management/commands/judge_worker.py",
        "core/management/commands/judge_admission.py",
        "core/management/commands/wasi_service.py",
        "core/management/commands/sandbox_check.py",
        "requirements.lock",
        "requirements-judge.lock",
        "uv.lock",
    ):
        fingerprint.update(relative.encode() + b"\0")
        fingerprint.update((root / relative).read_bytes())
    return fingerprint.hexdigest()


class DockerSandbox:
    def __init__(self, image):
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
            raise SandboxUnavailable("Judge image must be pinned by registry digest.")
        self.image = image

    def command(self, args, timeout=15):
        try:
            result = subprocess.run(
                ["docker", *args],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable("Docker control plane unavailable.") from exc
        if result.returncode != 0:
            # Docker messages may contain infrastructure details; not user feedback.
            raise SandboxUnavailable(f"Docker operation failed: {args[0]}")
        return result.stdout

    def preflight(self):
        info = json.loads(self.command(["info", "--format", "{{json .}}"], timeout=10))
        runtime = info.get("Runtimes", {}).get("runsc", {})
        if Path(runtime.get("path", "")).name != "runsc":
            raise SandboxUnavailable("A runsc runtime must be registered by the operator.")
        for feature in ("MemoryLimit", "SwapLimit", "PidsLimit", "CPUCfsQuota"):
            if not info.get(feature):
                raise SandboxUnavailable(f"Required resource controller unavailable: {feature}")
        image_info = json.loads(self.command(["image", "inspect", self.image]))[0]
        requested_digest = self.image.rsplit("@", 1)[1]
        if not any(
            item.rsplit("@", 1)[-1] == requested_digest
            for item in image_info.get("RepoDigests", [])
        ):
            raise SandboxUnavailable("Image digest does not match the locally installed image.")
        if image_info["Config"].get("Volumes"):
            raise SandboxUnavailable("Judge image must not declare volumes.")
        identity = {
            "policy": POLICY_VERSION,
            "policy_digest": policy_digest(),
            "image": self.image,
            "docker": info.get("ServerVersion"),
            "kernel": info.get("KernelVersion"),
            "runtime": runtime,
            "image_id": image_info["Id"],
            "architecture": image_info["Architecture"],
        }
        return identity

    def create_args(self, name):
        return [
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            "praximesh.judge=true",
            "--runtime=runsc",
            "--network=none",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=32",
            f"--memory={MEMORY}",
            f"--memory-swap={MEMORY}",
            "--cpus=0.5",
            "--ipc=none",
            "--cgroupns=private",
            "--log-driver=none",
            "--ulimit=nofile=64:64",
            "--ulimit=fsize=1048576:1048576",
            "--ulimit=cpu=2:2",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
            "--workdir=/tmp",
            "--interactive",
            "--attach=stdin",
            "--attach=stdout",
            "--attach=stderr",
            "--entrypoint=/usr/local/bin/python3",
            self.image,
            "-I",
            "-B",
            "-c",
            GUEST,
        ]

    def validate_container(self, details):
        host = details["HostConfig"]
        config = details["Config"]
        required = {
            "Runtime": "runsc",
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Memory": MEMORY,
            "MemorySwap": MEMORY,
            "PidsLimit": 32,
            "NanoCpus": 500000000,
            "IpcMode": "none",
            "CgroupnsMode": "private",
        }
        if any(host.get(key) != value for key, value in required.items()):
            raise SandboxUnavailable("Effective container policy does not match required limits.")
        if config["User"] != "65534:65534" or host.get("Binds") or host.get("Devices"):
            raise SandboxUnavailable("Unexpected container privileges or mounts.")
        if host.get("CapAdd") or "ALL" not in host.get("CapDrop", []):
            raise SandboxUnavailable("Capabilities must be dropped.")
        if not any(
            x in {"no-new-privileges", "no-new-privileges:true"}
            for x in host.get("SecurityOpt", [])
        ):
            raise SandboxUnavailable("No-new-privileges is required.")
        if any(
            mount.get("Type") != "tmpfs" or mount.get("Destination") != "/tmp"
            for mount in details.get("Mounts", [])
        ):
            raise SandboxUnavailable("Unexpected guest filesystem access.")
        if host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,nodev,size=8m,mode=1777"}:
            raise SandboxUnavailable("Temporary storage limits were not applied.")
        limits = {item["Name"]: (item["Soft"], item["Hard"]) for item in host.get("Ulimits", [])}
        if any(
            limits.get(key) != value
            for key, value in {
                "cpu": (2, 2),
                "nofile": (64, 64),
                "fsize": (1048576, 1048576),
            }.items()
        ):
            raise SandboxUnavailable("Process limits were not applied.")
        if host.get("LogConfig", {}).get("Type") != "none":
            raise SandboxUnavailable("Guest persistent logging must be disabled.")

    def execute_program(self, source, stdin):
        raise SandboxUnavailable("Complete programs require the reviewed WASI backend.")

    def execute_javascript(self, source, function, args):
        raise SandboxUnavailable("JavaScript requires the reviewed WASI backend.")

    def execute(self, source, function, args):
        payload = json.dumps(
            {"source": source, "function": function, "args": args},
            allow_nan=False,
            ensure_ascii=False,
        ).encode()
        if len(payload) > 256000:
            raise SandboxUnavailable("Input exceeds judge protocol limit.")
        name = f"praximesh-{uuid.uuid4().hex}"
        created = False
        try:
            self.command(self.create_args(name))
            created = True
            details = json.loads(self.command(["inspect", name]))[0]
            self.validate_container(details)
            verdict, output, elapsed = self._start(name, payload)
            state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
            if state.get("Error") or state.get("Running"):
                raise SandboxUnavailable("Execution did not reach a valid terminal state.")
            if verdict:
                return Execution(verdict, elapsed_ms=elapsed)
            if state.get("OOMKilled") or state.get("ExitCode") == 4:
                return Execution("memory_limit", elapsed_ms=elapsed)
            exitcode = state.get("ExitCode")
            if exitcode in {137, 152}:
                return Execution("time_limit", elapsed_ms=elapsed)
            if exitcode == 3:
                return Execution("syntax_error", elapsed_ms=elapsed)
            if exitcode != 0:
                return Execution("runtime_error", elapsed_ms=elapsed)
            try:
                envelope = json.loads(output)
                if not isinstance(envelope, dict) or set(envelope) != {"value"}:
                    raise ValueError("Invalid envelope")
            except (ValueError, UnicodeDecodeError, RecursionError):
                return Execution("runtime_error", elapsed_ms=elapsed)
            return Execution("ok", envelope["value"], elapsed)
        finally:
            # A control-plane failure after creation may leave a container alive;
            # cleanup failure propagates, stops worker admission and needs ops action.
            if created:
                self.command(["rm", "--force", name])

    def _start(self, name, payload):
        started = time.monotonic()
        process = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = bytearray()
        total = 0
        sent = 0
        verdict = ""
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr, process.stdin):
                    os.set_blocking(stream.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ, "out")
                selector.register(process.stderr, selectors.EVENT_READ, "err")
                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
                while selector.get_map():
                    if time.monotonic() - started > WALL_SECONDS:
                        verdict = "time_limit"
                        break
                    for key, _ in selector.select(timeout=0.1):
                        if key.data == "in":
                            try:
                                sent += os.write(key.fd, payload[sent : sent + 4096])
                            except BrokenPipeError:
                                sent = len(payload)
                            if sent == len(payload):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > OUTPUT:
                            verdict = "output_limit"
                            break
                        if key.data == "out":
                            stdout.extend(chunk)
                    if verdict:
                        break
            if verdict:
                self._kill_if_running(name)
            process.wait(timeout=3)
            if process.returncode and not verdict:
                # Guest exit code is inspected separately. Failed attach is internal.
                state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
                if state.get("ExitCode") == 0 or state.get("Running"):
                    raise SandboxUnavailable("Docker attach failed.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable("Execution transport failed.") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        return verdict, bytes(stdout), int((time.monotonic() - started) * 1000)

    def _kill_if_running(self, name):
        state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
        if not state.get("Running"):
            return
        try:
            self.command(["kill", name])
        except SandboxUnavailable:
            state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
            if state.get("Running") or state.get("Error"):
                raise
