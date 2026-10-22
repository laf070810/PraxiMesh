"""Build a NEW combined CPython/QuickJS bundle from pinned trusted inputs.

The CPython base comes from build_wasi.py or an existing immutable format-2
bundle. Its native cache is never loaded or reused: both caches are regenerated
with the current engine policy. Learner code is never compiled on the host.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from judge.wasi_policy import (  # noqa: E402
    BUNDLE_FILES,
    LANGUAGES,
    PYTHON_BUNDLE_FILES,
    PYTHON_VERSION,
    QUICKJS_VERSION,
    TARGET,
    WASMTIME_VERSION,
    engine_config,
)

SOURCES = {
    "quickjs": {
        "url": "https://bellard.org/quickjs/quickjs-2026-06-04.tar.xz",
        "sha256": "b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a",
    },
    "wasi_sdk": {
        "url": "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz",
        "sha256": "c6c38aab56e5de88adf6c1ebc9c3ae8da72f88ec2b656fb024eda8d4167a0bc5",
    },
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_python_base(base, digest):
    manifest = base / "manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 16000 or sha256(manifest) != digest:
        raise ValueError("Python base manifest differs from the explicitly trusted digest")
    info = json.loads(manifest.read_text())
    if (
        info["format"] != 2
        or info["python"] != PYTHON_VERSION
        or info["wasmtime"] != WASMTIME_VERSION
        or info["target"] != TARGET
        or info["architecture"] != "x86_64"
        or info["wasi_sdk"] != "24"
        or set(info["files"]) != set(PYTHON_BUNDLE_FILES)
    ):
        raise ValueError("Unsupported CPython base")
    actual = set()
    for path in [base, *base.rglob("*")]:
        if path.is_symlink() or path.stat().st_mode & 0o022:
            raise ValueError("Python base must be immutable regular files/directories")
        if path.is_file():
            actual.add(path.relative_to(base).as_posix())
        elif not path.is_dir():
            raise ValueError("Unsupported Python base filesystem entry")
    if actual != set(PYTHON_BUNDLE_FILES) | {"manifest.json"}:
        raise ValueError("Unexpected Python base contents")
    for name in PYTHON_BUNDLE_FILES:
        if (base / name).stat().st_size > 64 * 1024 * 1024 or sha256(base / name) != info["files"][
            name
        ]:
            raise ValueError("Python base file digest mismatch")
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-bundle", type=Path, required=True)
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--sdk-archive", type=Path, required=True)
    parser.add_argument("--quickjs-archive", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        parser.error("This build targets Linux x86_64.")
    if importlib.metadata.version("wasmtime") != WASMTIME_VERSION:
        parser.error("Use the locked judge dependencies.")
    output = options.output.resolve()
    if output.exists():
        parser.error("Output exists; never overwrite a deployed bundle.")
    base = options.python_bundle.resolve()
    base_info = verify_python_base(base, options.python_sha256)
    inputs = {"quickjs": options.quickjs_archive, "wasi_sdk": options.sdk_archive}
    for name, archive in inputs.items():
        if sha256(archive) != SOURCES[name]["sha256"]:
            raise ValueError(f"Pinned source digest mismatch: {name}")
    work = options.workdir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    for archive in inputs.values():
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
    source = work / f"quickjs-{QUICKJS_VERSION}"
    sdk = work / "wasi-sdk-24.0-x86_64-linux"
    patch = ROOT / "vendor/quickjs/wasi.patch"
    bridge = ROOT / "judge/javascript_bridge.c"
    subprocess.run(
        ["patch", "-p1", "--fuzz=0", "--input", str(patch)], cwd=source, check=True, timeout=15
    )
    shutil.copyfile(bridge, source / "javascript_bridge.c")
    if sha256(source / "LICENSE") != sha256(ROOT / "vendor/quickjs/LICENSE"):
        raise ValueError("QuickJS license differs from pinned vendor copy")
    # Bound compiler/cache work on shared build hosts; no guest code here.
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    os.environ["TOKIO_WORKER_THREADS"] = "1"
    os.environ["RAYON_NUM_THREADS"] = "1"
    command = [
        str(sdk / "bin/clang"),
        "--sysroot=" + str(sdk / "share/wasi-sysroot"),
        "-O2",
        "-D_GNU_SOURCE",
        f'-DCONFIG_VERSION="{QUICKJS_VERSION}"',
        "-Wl,-z,stack-size=1048576",
        "-Wl,--max-memory=134217728",
        "-o",
        "quickjs.wasm",
        "javascript_bridge.c",
        "quickjs.c",
        "dtoa.c",
        "libregexp.c",
        "libunicode.c",
        "cutils.c",
        "-lm",
    ]
    with (work / "build.log").open("w") as log:
        subprocess.run(
            command, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".functions-build-") as directory:
        bundle = Path(directory) / "bundle"
        (bundle / "stdlib").mkdir(parents=True)
        for name in PYTHON_BUNDLE_FILES:
            if name != "python.cwasm":
                shutil.copyfile(base / name, bundle / name)
        # Both interpreters use SDK 24's wasi-libc/compiler-rt; reuse its reviewed notices.
        notices = json.loads((ROOT / "vendor/wasi/sources.json").read_text())
        if notices["combined_sha256"] != sha256(bundle / "THIRD_PARTY_NOTICES.txt"):
            raise ValueError("WASI runtime notice provenance mismatch")
        shutil.copyfile(source / "quickjs.wasm", bundle / "quickjs.wasm")
        shutil.copyfile(source / "LICENSE", bundle / "LICENSE.quickjs")
        import wasmtime

        with wasmtime.Engine(engine_config()) as engine:
            for name in ("python", "quickjs"):
                with wasmtime.Module.from_file(engine, str(bundle / f"{name}.wasm")) as module:
                    (bundle / f"{name}.cwasm").write_bytes(module.serialize())
        manifest = {
            "format": 3,
            "python": PYTHON_VERSION,
            "quickjs": QUICKJS_VERSION,
            "languages": list(LANGUAGES),
            "wasmtime": WASMTIME_VERSION,
            "wasi_sdk": "24",
            "architecture": "x86_64",
            "target": TARGET,
            "sources": SOURCES,
            "python_sources": base_info["sources"],
            "python_base": options.python_sha256,
            "javascript_bridge": sha256(bridge),
            "quickjs_patch": sha256(patch),
            "engine_policy": sha256(ROOT / "judge/wasi_policy.py"),
            "files": {name: sha256(bundle / name) for name in BUNDLE_FILES},
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        for path in bundle.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        bundle.rename(output)
    print(f"JUDGE_WASI_BUNDLE={output}")
    print(f"JUDGE_WASI_SHA256={sha256(output / 'manifest.json')}")


if __name__ == "__main__":
    main()
