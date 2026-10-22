"""Build the reviewed CPython/WASI bundle from pinned official releases.

Run with the locked judge dependency group. Build code is trusted; no submitted
program is involved. Generated native Wasmtime cache must be treated like an
executable, never supplied by learners or loaded from a shared writable cache.
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
import sysconfig
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.wasi_policy import (  # noqa: E402
    BUNDLE_FILES,
    PYTHON_VERSION,
    TARGET,
    WASMTIME_VERSION,
    engine_config,
)

SOURCES = {
    "python": (
        "https://www.python.org/ftp/python/3.14.9/Python-3.14.9.tar.xz",
        "e02d9ac21cebda6e46435369744a92c65cc7cd8669697694122cf97ffacd1e53",
    ),
    "sdk": (
        "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-24/wasi-sdk-24.0-x86_64-linux.tar.gz",
        "c6c38aab56e5de88adf6c1ebc9c3ae8da72f88ec2b656fb024eda8d4167a0bc5",
    ),
    "cli": (
        "https://github.com/bytecodealliance/wasmtime/releases/download/v50.0.0/wasmtime-v50.0.0-x86_64-linux.tar.xz",
        "77c19ee0437991f9ac7837b9c269d9c7076744f85dad263fea5214cc2723c7ff",
    ),
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--workdir", type=Path, required=True, help="New disposable build directory"
    )
    parser.add_argument("--jobs", type=int, default=4, choices=range(1, 9))
    options = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        parser.error("This reviewed build recipe targets Linux x86_64.")
    if importlib.metadata.version("wasmtime") != WASMTIME_VERSION:
        parser.error("Use the locked judge dependency group.")
    output = options.output.resolve()
    if output.exists():
        parser.error(
            "Output already exists; build a new version without overwriting deployed artifacts."
        )
    work = options.workdir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    for name, (url, expected) in SOURCES.items():
        archive = work / url.rsplit("/", 1)[-1]
        print(f"Downloading and verifying {name}", flush=True)
        with urllib.request.urlopen(url, timeout=60) as source, archive.open("wb") as target:
            shutil.copyfileobj(source, target)
        if sha256(archive) != expected:
            raise ValueError(f"Digest mismatch: {name}")
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
    source = work / f"Python-{PYTHON_VERSION}"
    env = dict(os.environ)
    env.update(
        {
            "WASI_SDK_PATH": str(work / "wasi-sdk-24.0-x86_64-linux"),
            "TERM": "dumb",
            "SOURCE_DATE_EPOCH": "1792540800",
        }
    )
    env["PATH"] = str(work / "wasmtime-v50.0.0-x86_64-linux") + os.pathsep + env["PATH"]
    native = "cross-build/" + sysconfig.get_config_var("BUILD_GNU_TYPE")
    commands = [
        [
            sys.executable,
            "Tools/wasm/wasi",
            "configure-build-python",
            "--",
            "--without-ensurepip",
            "--disable-test-modules",
        ],
        ["make", f"-j{options.jobs}", "-C", native, "all"],
        [
            sys.executable,
            "Tools/wasm/wasi",
            "configure-host",
            "--host-triple=wasm32-wasip1",
            "--",
            "--prefix=/python",
            "--without-ensurepip",
            "--disable-test-modules",
        ],
        ["make", f"-j{options.jobs}", "-C", "cross-build/wasm32-wasip1", "all"],
    ]
    with (work / "build.log").open("w") as log:
        for command in commands:
            print("Building:", " ".join(command), flush=True)
            subprocess.run(
                command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".wasi-build-") as temporary:
        bundle = Path(temporary) / "bundle"
        (bundle / "stdlib").mkdir(parents=True)
        shutil.copyfile(source / "cross-build/wasm32-wasip1/python.wasm", bundle / "python.wasm")
        subprocess.run(
            [
                str(work / "wasi-sdk-24.0-x86_64-linux/bin/llvm-strip"),
                "--strip-debug",
                str(bundle / "python.wasm"),
            ],
            check=True,
        )
        shutil.copyfile(source / "LICENSE", bundle / "LICENSE.python")
        notices = Path(__file__).resolve().parent.parent / "vendor/wasi"
        provenance = json.loads((notices / "sources.json").read_text())
        if (
            provenance["wasi_sdk"] != "24"
            or sha256(notices / "LICENSES.txt") != provenance["combined_sha256"]
        ):
            raise ValueError("WASI runtime notice provenance mismatch")
        shutil.copyfile(notices / "LICENSES.txt", bundle / "THIRD_PARTY_NOTICES.txt")
        with zipfile.ZipFile(
            bundle / "stdlib/python314.zip", "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for file in sorted((source / "Lib").rglob("*")):
                relative = file.relative_to(source / "Lib")
                if (
                    file.is_file()
                    and not any(
                        part
                        in {
                            "test",
                            "tests",
                            "__pycache__",
                            "ensurepip",
                            "idlelib",
                            "tkinter",
                            "turtledemo",
                        }
                        for part in relative.parts
                    )
                    and file.suffix not in {".pyc", ".pyo"}
                ):
                    archive.writestr(
                        zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0)),
                        file.read_bytes(),
                    )
        import wasmtime

        with (
            wasmtime.Engine(engine_config()) as engine,
            wasmtime.Module.from_file(engine, str(bundle / "python.wasm")) as module,
        ):
            (bundle / "python.cwasm").write_bytes(module.serialize())
        manifest = {
            "format": 2,
            "python": PYTHON_VERSION,
            "wasmtime": WASMTIME_VERSION,
            "wasi_sdk": "24",
            "architecture": "x86_64",
            "target": TARGET,
            "sources": SOURCES,
            "engine_policy": sha256(
                Path(__file__).resolve().parent.parent / "judge/wasi_policy.py"
            ),
            "files": {name: sha256(bundle / name) for name in BUNDLE_FILES},
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        for file in bundle.rglob("*"):
            file.chmod(0o755 if file.is_dir() else 0o644)
        bundle.rename(output)
    print(f"JUDGE_WASI_BUNDLE={output}")
    print(f"JUDGE_WASI_SHA256={sha256(output / 'manifest.json')}")


if __name__ == "__main__":
    main()
