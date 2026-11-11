"""Run the fixed CI installer; classify failure in memory without uploading raw logs."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LOG_BYTES = 4 * 1024 * 1024

# Only these fixed labels may enter a public check annotation.
FAILURES = (
    (r"failed to fetch", "APT retrieval failed"),
    (r"some index files failed|does not have a release file", "APT index unavailable"),
    (r"temporary failure resolving|could not resolve", "DNS resolution failed"),
    (r"connection timed out|connection timeout", "network connection timed out"),
    (r"unable to connect|could not connect|network is unreachable", "network connection failed"),
    (
        r"\b(?:401 unauthorized|403 forbidden|404 not found|429 too many requests)\b",
        "HTTP client error",
    ),
    (
        r"\b(?:500 internal server error|502 bad gateway|503 service unavailable|504 gateway timeout)\b",
        "HTTP server error",
    ),
    (r"unable to locate package|has no installation candidate", "required package unavailable"),
    (r"unmet dependencies|held broken packages", "package dependency resolution failed"),
    (
        r"no_pubkey|expkeysig|signatures couldn't be verified|repository .* is not signed",
        "repository signature rejected",
    ),
    (r"could not get lock|unable to acquire .*lock", "package manager lock unavailable"),
    (r"no space left on device", "disk space exhausted"),
    (r"permission denied|are you root", "required installation permission unavailable"),
)
REPOSITORIES = (
    (r"https?://(?:[a-z0-9-]+\.)?archive\.ubuntu\.com(?:[:/]|$)", "Ubuntu archive"),
    (r"https?://security\.ubuntu\.com(?:[:/]|$)", "Ubuntu security"),
    (r"https?://packages\.microsoft\.com(?:[:/]|$)", "Microsoft repository"),
    (r"https?://apt\.postgresql\.org(?:[:/]|$)", "PostgreSQL repository"),
    (r"https?://download\.docker\.com(?:[:/]|$)", "Docker repository"),
    (r"https?://ppa\.launchpadcontent\.net(?:[:/]|$)", "Launchpad repository"),
)


def classifications(text):
    labels = [label for pattern, label in FAILURES if re.search(pattern, text, re.I)]
    errors = "\n".join(line for line in text.splitlines() if re.match(r"\s*(?:Err:|E:|W:)", line))
    labels.extend(label for pattern, label in REPOSITORIES if re.search(pattern, errors, re.I))
    return labels


def annotation(summary):
    return f"\n::error title=Chromium system dependencies::{summary}.\n".encode()


def run_installer(command, output):
    captured = bytearray()
    exceeded = False
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        output.write(annotation("Installer could not start"))
        return 127
    with process:
        while chunk := process.stdout.read1(8192):
            # Preserve the existing job output; never create an extra log artifact.
            output.write(chunk)
            output.flush()
            remaining = MAX_LOG_BYTES - len(captured)
            captured.extend(chunk[:remaining])
            exceeded |= len(chunk) > remaining
    code = 128 - process.returncode if process.returncode < 0 else process.returncode
    if code:
        labels = classifications(captured.decode("utf-8", errors="replace"))
        if exceeded:
            labels.append("diagnostic capture limit reached")
        output.write(annotation("; ".join(labels) if labels else "Unclassified installer failure"))
    return code


def main():
    return run_installer(
        [str(ROOT / ".venv/bin/playwright"), "install-deps", "chromium"], sys.stdout.buffer
    )


if __name__ == "__main__":
    raise SystemExit(main())
