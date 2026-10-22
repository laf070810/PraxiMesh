"""Capability-limited CPython/WASI. This is never a host-Python fallback."""

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from judge.sandbox import Execution, SandboxUnavailable, policy_digest
from judge.wasi_policy import (
    BUNDLE_FILES,
    INPUT,
    OUTPUT,
    POLICY_VERSION,
    PYTHON_VERSION,
    TARGET,
    WALL_SECONDS,
    WASMTIME_VERSION,
)


class WasiSandbox:
    def __init__(self, bundle, digest):
        if not bundle or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise SandboxUnavailable("WASI bundle and manifest SHA-256 must be configured.")
        self.bundle = Path(bundle).resolve()
        self.digest = digest

    def verify_bundle(self):
        try:
            if self.bundle.stat().st_mode & 0o022:
                raise ValueError("Artifact directory must not be group/world writable")
            manifest = self.bundle / "manifest.json"
            if manifest.stat().st_size > 16000 or manifest.is_symlink():
                raise ValueError("Invalid manifest")
            data = manifest.read_bytes()
            if hashlib.sha256(data).hexdigest() != self.digest:
                raise ValueError("Manifest digest mismatch")
            info = json.loads(data)
            if info["format"] != 2 or info["python"] != PYTHON_VERSION:
                raise ValueError("Unsupported artifact")
            if info["wasmtime"] != WASMTIME_VERSION or info["architecture"] != "x86_64":
                raise ValueError("Compiled artifact runtime mismatch")
            if info["target"] != TARGET:
                raise ValueError("Compiled artifact target mismatch")
            policy = Path(__file__).with_name("wasi_policy.py").read_bytes()
            if info["engine_policy"] != hashlib.sha256(policy).hexdigest():
                raise ValueError("Rebuild the interpreter cache after engine policy changes")
            expected = set(BUNDLE_FILES)
            if set(info["files"]) != expected:
                raise ValueError("Unexpected artifact files")
            files = set()
            for path in self.bundle.rglob("*"):
                if path.is_symlink() or path.stat().st_mode & 0o022:
                    raise ValueError("Symlinks or group/world writable artifacts are forbidden")
                if path.is_file():
                    files.add(path.relative_to(self.bundle).as_posix())
                elif not path.is_dir():
                    raise ValueError("Only regular artifact files are allowed")
            if files != expected | {"manifest.json"}:
                raise ValueError("Unexpected artifact contents")
            for relative, digest in info["files"].items():
                path = self.bundle / relative
                if path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("Artifact exceeds policy size")
                with path.open("rb") as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                        raise ValueError("Artifact digest mismatch")
            return info
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SandboxUnavailable("WASI artifact verification failed.") from exc

    def preflight(self):
        if sys.platform != "linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
            raise SandboxUnavailable("WASI policy requires a non-root Linux x86_64 worker.")
        try:
            version = importlib.metadata.version("wasmtime")
        except importlib.metadata.PackageNotFoundError as exc:
            raise SandboxUnavailable("Install the locked judge dependency group.") from exc
        if version != WASMTIME_VERSION:
            raise SandboxUnavailable("Wasmtime version differs from the reviewed policy.")
        self.runtime_file = Path(
            importlib.metadata.distribution("wasmtime").locate_file(
                "wasmtime/linux-x86_64/_libwasmtime.so"
            )
        )
        with self.runtime_file.open("rb") as library:
            self.runtime_digest = hashlib.file_digest(library, "sha256").hexdigest()
        self.verify_bundle()
        return {
            "policy": POLICY_VERSION,
            "policy_digest": policy_digest(),
            "bundle": self.digest,
            "python": PYTHON_VERSION,
            "wasmtime": version,
            "wasmtime_library": self.runtime_digest,
            "driver_python": platform.python_version(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "host": platform.node(),
        }

    def execute(self, source, function, args):
        self.verify_bundle()
        if not hasattr(self, "runtime_digest"):
            raise SandboxUnavailable("WASI preflight is required before execution.")
        with self.runtime_file.open("rb") as library:
            if hashlib.file_digest(library, "sha256").hexdigest() != self.runtime_digest:
                raise SandboxUnavailable("Wasmtime changed while the worker was running.")
        payload = json.dumps(
            {"source": source, "function": function, "args": args},
            allow_nan=False,
            ensure_ascii=False,
        ).encode()
        if len(payload) > INPUT:
            raise SandboxUnavailable("Input exceeds judge protocol limit.")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="praximesh-wasi-") as directory:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).with_name("wasi_runner.py")),
                    str(self.bundle),
                    str(os.getpid()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=directory,
                env={"LANG": "C.UTF-8", "TMPDIR": directory},
                start_new_session=True,
                close_fds=True,
            )
            output = bytearray()
            total = sent = 0
            ready = False
            verdict = ""
            try:
                with selectors.DefaultSelector() as selector:
                    for stream, kind in (
                        (process.stdin, "in"),
                        (process.stdout, "out"),
                        (process.stderr, "err"),
                    ):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(
                            stream,
                            selectors.EVENT_WRITE if kind == "in" else selectors.EVENT_READ,
                            kind,
                        )
                    while selector.get_map():
                        if time.monotonic() - started > WALL_SECONDS:
                            if not ready:
                                raise SandboxUnavailable("WASI driver initialization timed out.")
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
                            if total > OUTPUT + 8192:
                                raise SandboxUnavailable("WASI driver exceeded transport limit.")
                            if key.data == "out":
                                output.extend(chunk)
                            elif chunk == b"R" and not ready:
                                ready = True
                            else:
                                raise SandboxUnavailable("WASI driver transport failed.")
                if verdict:
                    self._kill(process)
                process.wait(timeout=3)
            finally:
                self._kill(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream and not stream.closed:
                        stream.close()
        elapsed = int((time.monotonic() - started) * 1000)
        if not ready:
            raise SandboxUnavailable("WASI driver initialization failed.")
        verdict = verdict or {
            71: "time_limit",
            72: "output_limit",
            73: "syntax_error",
            74: "memory_limit",
            75: "runtime_error",
            -signal.SIGXCPU: "time_limit",
            -signal.SIGALRM: "time_limit",
        }.get(process.returncode, "")
        if verdict:
            return Execution(verdict, elapsed_ms=elapsed)
        if process.returncode != 0:
            raise SandboxUnavailable("WASI driver failed; execution is unavailable.")
        try:
            envelope = json.loads(output)
            if not isinstance(envelope, dict) or set(envelope) != {"value"}:
                raise ValueError("Invalid envelope")
        except (ValueError, UnicodeDecodeError, RecursionError):
            return Execution("runtime_error", elapsed_ms=elapsed)
        return Execution("ok", envelope["value"], elapsed)

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
