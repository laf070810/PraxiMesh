"""Fixed CUDA entry; reached only through the reviewed single-card runsc runtime."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.gpu_runtime import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
