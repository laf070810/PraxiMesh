"""Fixed credential-free Docker exec entrypoint; no arguments or diagnostics."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judge.container_transport import bridge  # noqa: E402

if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise ValueError("No bridge arguments are accepted")
        bridge()
    except Exception:
        raise SystemExit(70) from None
