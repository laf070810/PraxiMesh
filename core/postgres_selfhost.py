"""Run already-prepared PostgreSQL Compose application roles.

This entry neither initializes/migrates a database nor authorizes GPU execution.
The dedicated GPU worker keeps its own private configuration and startup suite.
"""

import os
import re
import sys

from core.preview import PreviewError
from core.selfhost import coaching_environment, worker_health
from judge.container_worker_config import database_settings
from judge.gpu_device_lock import GPU_UUID


def environment(role):
    if role not in {"web", "worker", "health"}:
        raise PreviewError("Unknown PostgreSQL Compose application role.")
    instance = os.environ.get("PRAXIMESH_COMPOSE_INSTANCE", "")
    generation = os.environ.get("PRAXIMESH_COMPOSE_GENERATION", "")
    hostname = os.environ.get("PRAXIMESH_COMPOSE_POSTGRES", "")
    secret = os.environ.get("APP_SECRET_KEY", "")
    digest = os.environ.get("JUDGE_WASI_SHA256", "")
    if (
        os.environ.get("PRAXIMESH_MODE", "local") != "local"
        or not re.fullmatch(r"[a-z][a-z0-9]{0,19}", instance)
        or not re.fullmatch(r"[a-f0-9]{12}", generation)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", hostname)
        or len(secret) < 32
        or secret.startswith("REPLACE")
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or os.environ.get("DB_SSLMODE", "disable") != "disable"
    ):
        raise PreviewError("A prepared local PostgreSQL Compose installation is required.")
    url = os.environ.get("DATABASE_URL", "")
    database = database_settings({"database": {"url": url, "sslmode": "disable"}})
    expected_role = "web" if role == "web" else "worker"
    if (
        database["NAME"] != f"px_{instance}_{generation}"
        or database["USER"] != f"px_{instance}_{expected_role}"
        or database["HOST"] != hostname
        or database["PORT"] != 5432
    ):
        raise PreviewError("PostgreSQL settings do not match the prepared application role.")
    result = dict(
        os.environ,
        PRAXIMESH_MODE="local",
        DATABASE_URL=url,
        DB_SSLMODE="disable",
        JUDGE_ENABLED="1",
        JUDGE_BACKEND="wasi",
        JUDGE_WASI_SHA256=digest,
        JUDGE_WASI_BUNDLE="/opt/praximesh/wasi",
        JUDGE_RECEIPT="/tmp/sandbox-receipt.json",
    )
    result.pop("SQLITE_PATH", None)
    if role == "web":
        device = result.get("JUDGE_GPU_DEVICES", "")
        if device and not GPU_UUID.fullmatch(device):
            raise PreviewError("Select one explicit physical GPU UUID for this installation.")
        return coaching_environment(result)
    result = {
        key: value
        for key, value in result.items()
        if not key.startswith(("COACHING_", "JUDGE_GPU_"))
    }
    result.update(COACHING_ENABLED="0", JUDGE_GPU_DEVICES="")
    return result


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "web"
    configured = environment(role)
    os.environ.clear()
    os.environ.update(configured)
    if role == "health":
        return worker_health()
    if role == "web":
        if os.environ.get("COACHING_ENABLED") == "1":
            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
            django.setup()
            from coaching.configuration import current

            current()
        command = [
            "gunicorn",
            "praximesh.wsgi:application",
            "--bind=0.0.0.0:8000",
            "--workers=1",
            "--threads=2",
            "--worker-tmp-dir=/tmp",
            "--access-logfile=-",
        ]
    else:
        command = [sys.executable, "manage.py", "wasi_service"]
    os.execvpe(command[0], command, dict(os.environ))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError):
        print(
            "PostgreSQL Compose startup failed; inspect the prepared private settings.",
            file=sys.stderr,
        )
        sys.exit(1)
