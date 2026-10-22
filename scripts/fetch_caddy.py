"""Fetch the digest-pinned official Caddy used by deployment acceptance."""

import argparse
import hashlib
import io
import os
import tarfile
import urllib.request
from pathlib import Path

VERSION = "2.11.7"
URL = f"https://github.com/caddyserver/caddy/releases/download/v{VERSION}/caddy_{VERSION}_linux_amd64.tar.gz"
SHA256 = "727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    if output.exists():
        parser.error("Refusing to overwrite an existing executable.")
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Caddy release digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        binary = archive.extractfile("caddy").read()
        license_text = archive.extractfile("LICENSE").read()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(binary)
    os.chmod(output, 0o755)
    output.with_name(output.name + ".LICENSE").write_bytes(license_text)
    print(f"Caddy {VERSION}: {output.resolve()}")
    print(f"Binary SHA-256: {hashlib.sha256(binary).hexdigest()}")


if __name__ == "__main__":
    main()
