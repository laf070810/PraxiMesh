"""Build and smoke-check a device-free local CUDA compiler; retain its image."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_compiler import compiler_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from scripts.build_container_compiler import build_image  # noqa: E402
from scripts.practice_cuda import compile_source  # noqa: E402

REFERENCE = """extern "C" __global__ void solve(
    const float* a, const float* b, float* out, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = a[i] + b[i];
}"""


def build(options):
    output = options.output
    if (
        os.getuid() == 0
        or not output.is_absolute()
        or ".." in output.parts
        or output.resolve() != output
    ):
        raise ValueError("Use a new absolute non-symlink output directory as non-root")
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    output.mkdir(mode=0o700)
    tag = "praximesh-local-compiler-" + uuid.uuid4().hex
    result = {
        "scope": "Device-free compiler build and compilation checks only",
        "checked": False,
        "formal_result": False,
        "executed": False,
        "tag": tag,
        "compiler_contract": compiler_digest(),
        "checks": [],
    }
    try:
        image, artifacts = build_image(
            engine, options.runtime_archive, options.nvrtc_archive, output, tag
        )
        result.update(image=image, artifacts=artifacts)
        for name, source, expected in (
            ("reference", REFERENCE, "compiled"),
            ("syntax-error", 'extern "C" __global__ void solve( {', "syntax_error"),
        ):
            checked = compile_source(
                source,
                "vector-add-f32-v1",
                [8, 6],
                state=output / "check-state",
                socket=options.docker_socket,
                image=image,
            )
            result["checks"].append({"name": name, **checked})
            if checked["status"] != expected:
                raise RuntimeError("Compiler smoke check failed")
        if result["compiler_contract"] != compiler_digest():
            raise RuntimeError("Compiler source changed during build checks")
        result["checked"] = True
        return result
    finally:
        with (output / "build-result.json").open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(result, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--nvrtc-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        result = build(options)
    except Exception:
        print(
            "Compiler build/check failed. Retain its output and check-state for diagnosis "
            "and recover-compiler; do not reuse an existing output directory.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
