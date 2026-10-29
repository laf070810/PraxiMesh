"""Bounded, retryable downloads of explicitly pinned build inputs."""

import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request


def download(url, path, digest, maximum=256 * 1024**2):
    if path.exists() or path.is_symlink():
        raise ValueError("Refusing to overwrite an existing build input")
    for attempt in range(3):
        try:
            checksum, size = hashlib.sha256(), 0
            deadline = time.monotonic() + 900
            with tempfile.NamedTemporaryFile(dir=path.parent) as temporary:
                with urllib.request.urlopen(url, timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > maximum or time.monotonic() > deadline:
                            raise ValueError("Pinned download exceeds size or time limit")
                        checksum.update(chunk)
                        temporary.write(chunk)
                if checksum.hexdigest() != digest:
                    raise ValueError("Pinned download digest mismatch")
                temporary.flush()
                os.fsync(temporary.fileno())
                os.link(temporary.name, path)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
