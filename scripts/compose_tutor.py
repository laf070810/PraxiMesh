"""Start the optional CPU-only tutor on the Web container's loopback interface."""

import os
import resource
import stat
from pathlib import Path


def main():
    key = Path("/ai/preview-secret.key")
    if (
        key.is_symlink()
        or not key.is_file()
        or key.stat().st_uid != os.getuid()
        or stat.S_IMODE(key.stat().st_mode) & 0o077
        or not 50 <= len(key.read_text()) <= 256
    ):
        raise ValueError("Local tutor requires the Web-generated private token.")
    # Inference uses two permitted CPUs and no GPU, even when the host has CUDA.
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[-2:])
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = [
        "llama-server",
        "--model",
        "/opt/tutor.gguf",
        "--alias",
        "praximesh-local-tutor",
        "--host",
        "127.0.0.1",
        "--port",
        "8081",
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
        "--log-disable",
    ]
    print("Starting the private CPU tutor; generated text and requests are not logged.", flush=True)
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
