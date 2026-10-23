"""Disposable local Chromium coaching check against an existing real model service.

The model is operator-owned and is never started/stopped here. Only public
synthetic exercise context is sent. Provider credentials are read from a private
JSON file, passed through the application environment, and never printed.
"""

import argparse
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-config", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    config = {}
    if args.provider_config:
        information = args.provider_config.lstat()
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_uid != os.getuid()
            or stat.S_IMODE(information.st_mode) != 0o600
        ):
            parser.error("Provider configuration must be an owner-private 0600 regular file")
        config = json.loads(args.provider_config.read_text())
    artifacts = root / "test-results"
    artifacts.mkdir(exist_ok=True)
    cleanup = False
    with tempfile.TemporaryDirectory(prefix="praximesh-coaching-") as temporary:
        env = dict(
            os.environ,
            PRAXIMESH_MODE="local",
            DATABASE_URL="",
            SQLITE_PATH=str(Path(temporary) / "app.sqlite3"),
            APP_SECRET_KEY=secrets.token_urlsafe(48),
            REGISTRATION_OPEN="1",
            APP_ALLOWED_HOSTS="127.0.0.1,localhost",
            JUDGE_ENABLED="0",
            APP_STATIC_ROOT=str(Path(temporary) / "static"),
            COACHING_ENABLED="1" if config else "0",
            COACHING_ENDPOINT=config.get("endpoint", ""),
            COACHING_MODEL=config.get("model", ""),
            COACHING_TOKEN=config.get("token", ""),
            COACHING_PROVIDER_DESCRIPTION="Local CPU model used for a synthetic acceptance check. Advice may be wrong and requires verification.",
            COACHING_ALLOW_LOOPBACK_HTTP="1" if config.get("allow_loopback_http") else "0",
            COACHING_DISABLE_THINKING="1" if config.get("disable_thinking") else "0",
            COACHING_MAX_TOKENS=str(config.get("max_tokens", 192)),
            COACHING_DAILY_LIMIT="5",
            COACHING_CONCURRENCY="1",
        )
        if config.get("ca_pem"):
            env["COACHING_CA_PEM"] = config["ca_pem"]
        if config.get("ca_file"):
            env["COACHING_CA_FILE"] = config["ca_file"]

        def run(*arguments):
            subprocess.run(
                [sys.executable, *arguments],
                cwd=root,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=90,
            )

        run("manage.py", "migrate", "--noinput", "--verbosity=0")
        run("manage.py", "content_import", "--translations", str(root / "content/en.json"))
        run("manage.py", "collectstatic", "--noinput", "--verbosity=0")
        with socket.socket() as allocated:
            allocated.bind(("127.0.0.1", 0))
            port = allocated.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (artifacts / "coaching-server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gunicorn",
                    "praximesh.wsgi:application",
                    "--bind",
                    f"127.0.0.1:{port}",
                    "--workers",
                    "1",
                    "--threads",
                    "2",
                    "--timeout",
                    "30",
                    "--config",
                    "gunicorn.conf.py",
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 20
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Disposable application exited; inspect the server log")
                    try:
                        with opener.open(base + "/health/", timeout=1) as response:
                            assert response.status == 200
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "Disposable application did not become ready"
                            ) from None
                        time.sleep(0.1)
                browser_env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("COACHING_")
                    and key
                    not in {
                        "BROWSER_TEST_SPKI",
                        "BROWSER_CA_FILE",
                        "BROWSER_COACHING_REAL",
                        "BROWSER_COACHING",
                    }
                }
                browser_env.update(
                    BROWSER_URL=base,
                    BROWSER_COACHING="1" if config else "0",
                    BROWSER_ARTIFACTS=str(artifacts),
                )
                subprocess.run(
                    [sys.executable, "tests/browser_coaching.py"],
                    cwd=root,
                    env=browser_env,
                    check=True,
                    timeout=180,
                )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                cleanup = server.poll() is not None
    (artifacts / "coaching-launcher.json").write_text(
        json.dumps(
            {
                "application_stopped": cleanup,
                "temporary_database_removed": not Path(temporary).exists(),
                "model_service_owned_by_launcher": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
