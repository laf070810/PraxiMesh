"""Configure the single-host Compose services before Django imports settings."""

import os
import re
import socket
import sys
from datetime import timedelta
from pathlib import Path

from core.preview import PreviewError, prepare, private_file, secret_key

DATA = Path("/data")
IDENTITY = Path("/opt/praximesh/wasi.sha256")
AI_DATA = Path("/ai")


def environment(role):
    if os.environ.get("PRAXIMESH_MODE", "local") != "local" or os.environ.get("DATABASE_URL"):
        raise PreviewError("Single-host Compose requires local SQLite mode.")
    digest = IDENTITY.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise PreviewError("Invalid packaged WASI identity.")
    result = dict(
        os.environ,
        PRAXIMESH_MODE="local",
        DATABASE_URL="",
        SQLITE_PATH=str(DATA / "praximesh.sqlite3"),
        JUDGE_ENABLED="1",
        JUDGE_BACKEND="wasi",
        JUDGE_WASI_SHA256=digest,
        JUDGE_WASI_BUNDLE="/opt/praximesh/wasi",
        JUDGE_RECEIPT="/tmp/sandbox-receipt.json",
    )
    if role != "web":
        result = {key: value for key, value in result.items() if not key.startswith("COACHING_")}
        result["COACHING_ENABLED"] = "0"
        private_file(DATA / "praximesh.sqlite3")
        if not result.get("APP_SECRET_KEY"):
            private_file(DATA / "preview-secret.key")
        result["APP_SECRET_KEY"] = secret_key(DATA, result.get("APP_SECRET_KEY", ""))
    elif result.get("PRAXIMESH_LOCAL_AI") == "1":
        if AI_DATA.is_symlink() or not AI_DATA.is_dir():
            raise PreviewError("Local tutor credentials require a private volume.")
        result.update(
            COACHING_ENABLED="1",
            COACHING_ENDPOINT="http://127.0.0.1:8081/v1/chat/completions",
            COACHING_MODEL="praximesh-local-tutor",
            COACHING_TOKEN=secret_key(AI_DATA, ""),
            COACHING_ALLOW_LOOPBACK_HTTP="1",
            COACHING_DISABLE_THINKING="1",
            COACHING_CA_FILE="",
            COACHING_CA_PEM="",
            COACHING_PROVIDER_DESCRIPTION=(
                "Self-hosted Qwen3-0.6B CPU tutor on this server. Selected input stays on this "
                "server; private hint history is saved in your account. Advice may be incorrect."
            ),
        )
    else:
        # Remote services must use HTTPS; the local exception belongs to the sidecar only.
        result["COACHING_ALLOW_LOOPBACK_HTTP"] = "0"
    return result


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "web"
    if role not in {"web", "worker", "health", "manage"}:
        raise PreviewError("Unknown Compose service role.")
    configured = environment(role)
    os.environ.clear()
    os.environ.update(configured)
    if role == "web":
        os.environ.update(prepare(DATA, services=True))
        if os.environ.get("COACHING_ENABLED") == "1":
            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
            django.setup()
            from coaching.configuration import current

            current()  # Validate without printing credentials or contacting a provider.
        args = [
            "gunicorn",
            "praximesh.wsgi:application",
            "--bind=0.0.0.0:8000",
            "--workers=1",
            "--threads=2",
            "--worker-tmp-dir=/tmp",
            "--access-logfile=-",
        ]
    elif role == "health":
        if not Path(os.environ["JUDGE_RECEIPT"]).is_file():
            return 1
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
        django.setup()
        from django.utils import timezone

        from core.models import WorkerHealth
        from core.queue import configured_fingerprint

        return (
            0
            if WorkerHealth.objects.filter(
                pk=socket.gethostname(),
                ready=True,
                checked_at__gte=timezone.now() - timedelta(seconds=30),
                fingerprint=configured_fingerprint(),
            ).exists()
            else 1
        )
    else:
        args = [
            sys.executable,
            "manage.py",
            *(sys.argv[2:] if role == "manage" else ["wasi_service"]),
        ]
    os.execvpe(args[0], args, dict(os.environ))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError):
        # Configurations can contain private endpoints and tokens; fail without dumping them.
        print(
            "Compose startup failed; check the configured data, artifact or tutor settings.",
            file=sys.stderr,
        )
        sys.exit(1)
