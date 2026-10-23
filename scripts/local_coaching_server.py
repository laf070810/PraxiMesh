"""Run a pinned llama.cpp binary and GGUF on loopback for bounded local tutor checks.

No download, public listener, GPU workload or permanent service is created.
The private connection file exists only while this process owns the model server.
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import resource
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--binary-sha256", required=True)
parser.add_argument("--model", type=Path, required=True)
parser.add_argument("--model-sha256", required=True)
parser.add_argument("--state", type=Path, required=True, help="A new private directory")
parser.add_argument(
    "--cpu", type=int, action="append", required=True, help="Exactly two permitted CPU indices"
)
parser.add_argument("--lifetime", type=int, default=600)
options = parser.parse_args()
if (
    len(options.cpu) != 2
    or len(set(options.cpu)) != 2
    or not set(options.cpu) <= os.sched_getaffinity(0)
):
    parser.error("Select exactly two distinct CPUs in the current affinity")
if not 30 <= options.lifetime <= 1800:
    parser.error("Lifetime must be 30–1800 seconds")
MODEL = options.model.resolve(strict=True)
BINARY = options.binary.resolve(strict=True)
for path, expected in ((MODEL, options.model_sha256), (BINARY, options.binary_sha256)):
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or not path.is_file():
        parser.error("Artifacts require a regular file and trusted SHA-256")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            parser.error("Artifact digest mismatch")
ROOT = options.state.absolute()
ROOT.mkdir(mode=0o700, exist_ok=False)
CONNECTION = ROOT / "runtime-connection.json"
STOP = ROOT / "stop-runtime"
PARENT = os.getpid()


def stop(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop)


def limits():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != PARENT:
        os._exit(125)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        os._exit(125)
    os.sched_setaffinity(0, set(options.cpu))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (300, 310))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


with tempfile.TemporaryDirectory(prefix="px-ai-private-", dir=ROOT) as temporary:
    private = Path(temporary)
    token = secrets.token_urlsafe(32)
    key = private / "api-key"
    key.write_text(token + "\n")
    key.chmod(0o600)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    config = {
        "endpoint": base + "/v1/chat/completions",
        "model": "praximesh-local-tutor",
        "token": token,
        "allow_loopback_http": True,
        "disable_thinking": True,
        "timeout_seconds": 20,
        "max_tokens": 192,
    }
    args = [
        str(BINARY),
        "--model",
        str(MODEL),
        "--alias",
        config["model"],
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--api-key-file",
        str(key),
        "--threads",
        "2",
        "--threads-batch",
        "2",
        "--threads-http",
        "2",
        "--parallel",
        "1",
        "--ctx-size",
        "2048",
        "--n-predict",
        "192",
        "--n-gpu-layers",
        "0",
        "--no-op-offload",
        "--offline",
        "--no-agent",
        "--no-webui",
        "--no-ui-mcp-proxy",
        "--no-slots",
        "--no-cache-prompt",
        "--cache-ram",
        "0",
        "--no-cache-idle-slots",
        "--no-context-shift",
        "--reasoning",
        "off",
        "--reasoning-budget",
        "0",
        "--timeout",
        "25",
        "--no-cors-credentials",
        "--cors-origins",
        "",
        "--log-verbosity",
        "1",
    ]
    begin = time.monotonic()
    lifetime_deadline = begin + options.lifetime
    startup_deadline = min(begin + 90, lifetime_deadline)
    with (ROOT / "runtime.log").open("wb") as log:
        child = subprocess.Popen(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={"OMP_NUM_THREADS": "2"},
            close_fds=True,
            start_new_session=True,
            preexec_fn=limits,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < startup_deadline:
                if child.poll() is not None:
                    raise RuntimeError("Owned CPU model service exited during startup")
                try:
                    remaining = startup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Owned CPU model service startup deadline")
                    with opener.open(base + "/health", timeout=min(1, remaining)) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(max(0, min(0.25, startup_deadline - time.monotonic())))
            else:
                raise RuntimeError("Owned CPU model service startup deadline")
            if time.monotonic() >= startup_deadline:
                raise RuntimeError("Owned CPU model service startup deadline")
            with CONNECTION.open("x") as output:
                os.chmod(CONNECTION, 0o600)
                json.dump(config, output)
            (ROOT / "runtime-identity.json").write_text(
                json.dumps(
                    {
                        "model_sha256": options.model_sha256,
                        "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
                        "cpu_only": True,
                        "affinity": options.cpu,
                        "model_threads": 2,
                        "parallel_slots": 1,
                        "context_tokens": 2048,
                        "prediction_tokens": 192,
                        "address": base,
                        "tools_enabled": False,
                        "web_ui_enabled": False,
                        "memory_limit_bytes": 3 * 1024**3,
                        "total_cpu_soft_seconds": 300,
                        "lifetime_limit_seconds": options.lifetime,
                    },
                    indent=2,
                )
                + "\n"
            )
            print("Owned CPU-only model ready; private configuration written", flush=True)
            while time.monotonic() < lifetime_deadline and not STOP.exists():
                if child.poll() is not None:
                    raise RuntimeError("Owned CPU model service exited")
                time.sleep(max(0, min(1, lifetime_deadline - time.monotonic())))
        finally:
            CONNECTION.unlink(missing_ok=True)
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    # At the lifetime deadline, escalate immediately rather than
                    # granting an additional five seconds to a live model process.
                    child.wait(timeout=max(0, min(5, lifetime_deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
            STOP.unlink(missing_ok=True)
            (ROOT / "runtime-cleanup.json").write_text(
                json.dumps(
                    {
                        "process_terminal": child.poll() is not None,
                        "private_connection_removed": not CONNECTION.exists(),
                        "elapsed_seconds": round(time.monotonic() - begin, 3),
                    },
                    indent=2,
                )
                + "\n"
            )
print("Owned CPU model service stopped and private key removed", flush=True)
