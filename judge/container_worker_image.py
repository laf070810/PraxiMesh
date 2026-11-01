"""Frozen application sources for the trusted container worker image."""

import hashlib
from pathlib import Path

from judge.container_controller import CONTROLLER_SOURCES, controller_digest

ROOT = Path(__file__).resolve().parents[1]
APP_DIRECTORIES = (
    "praximesh",
    "core",
    "learning",
    "community",
    "practice_api",
    "showcase",
    "localization",
    "projects",
    "organizations",
    "coaching",
    "challenges",
    "judge",
)
EXTRA_SOURCES = (
    "scripts/container_worker_entry.py",
    "scripts/build_container_worker.py",
    "scripts/build_container_controller.py",
    "scripts/configure_container_worker.py",
    "scripts/deploy_database.py",
    "deploy/container-worker.Dockerfile",
    "deploy/compose.gpu-worker.yaml",
    "manage.py",
    "requirements.lock",
    "pyproject.toml",
    "uv.lock",
    "LICENSES.md",
)


def source_files(root=ROOT):
    names = set(CONTROLLER_SOURCES) | set(EXTRA_SOURCES)
    for directory in APP_DIRECTORIES:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError("Worker application directory is missing or linked")
        names.update(path.relative_to(root).as_posix() for path in base.rglob("*.py"))
    for name in names:
        path = root / name
        if not path.is_file() or path.resolve() != path or path.stat().st_size > 10 * 1024**2:
            raise ValueError("Worker source is missing, linked or oversized")
    return tuple(sorted(names))


def worker_digest(root=ROOT):
    digest = hashlib.sha256()
    for name in source_files(root):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()


def verify_image():
    if (ROOT / "worker-contract").read_text().strip() != worker_digest() or (
        ROOT / "controller-contract"
    ).read_text().strip() != controller_digest():
        raise ValueError("Container worker image source identity changed")
