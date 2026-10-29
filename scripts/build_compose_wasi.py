"""Build the trusted WASI bundle inside Docker; no pre-existing host artifacts."""

import subprocess
import sys
from pathlib import Path

from build_functions_wasi import SOURCES
from build_wasi import sha256
from fetch_verified import download


def main():
    base, work, output = Path("/build/python"), Path("/build/cpython"), Path("/build/wasi")
    subprocess.run(
        [
            sys.executable,
            "scripts/build_wasi.py",
            "--output",
            str(base),
            "--workdir",
            str(work),
            "--jobs",
            "2",
        ],
        check=True,
    )
    archive = Path("/build/quickjs.tar.xz")
    download(SOURCES["quickjs"]["url"], archive, SOURCES["quickjs"]["sha256"])
    # build_functions_wasi verifies the downloaded archive before extraction.
    subprocess.run(
        [
            sys.executable,
            "scripts/build_functions_wasi.py",
            "--python-bundle",
            str(base),
            "--python-sha256",
            sha256(base / "manifest.json"),
            "--sdk-archive",
            str(work / "wasi-sdk-24.0-x86_64-linux.tar.gz"),
            "--quickjs-archive",
            str(archive),
            "--workdir",
            "/build/functions",
            "--output",
            str(output),
        ],
        check=True,
    )
    Path("/build/wasi.sha256").write_text(sha256(output / "manifest.json") + "\n")


if __name__ == "__main__":
    main()
