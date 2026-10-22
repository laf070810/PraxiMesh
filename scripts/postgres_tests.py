"""Run the full test suite against an owned, temporary loopback PostgreSQL instance."""

import os
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.production_acceptance import POSTGRES  # noqa: E402
from scripts.release import docker  # noqa: E402
from scripts.release_acceptance import start_container  # noqa: E402


def main():
    token = uuid.uuid4().hex[:12]
    name = "praximesh-regression-" + token
    volume = name + "-data"
    password = secrets.token_urlsafe(32)
    with ExitStack() as stack:
        docker(
            "volume",
            "create",
            "--label",
            "org.praximesh.acceptance=" + token,
            volume,
            capture_output=True,
        )
        stack.callback(docker, "volume", "rm", volume, capture_output=True)
        start_container(
            stack,
            name,
            [
                "--name",
                name,
                "--memory=512m",
                "--pids-limit=128",
                "--mount",
                f"type=volume,src={volume},dst=/var/lib/postgresql/data",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                POSTGRES,
            ],
            dict(os.environ, POSTGRES_PASSWORD=password),
        )
        port = int(
            docker("port", name, "5432/tcp", capture_output=True).stdout.strip().rsplit(b":", 1)[1]
        )
        url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres?sslmode=disable"
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(url, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Temporary PostgreSQL did not become ready") from None
                time.sleep(0.2)
        environment = dict(
            os.environ, DATABASE_URL=url, DB_SSLMODE="disable", PRAXIMESH_MODE="local"
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *sys.argv[1:]],
            cwd=ROOT,
            env=environment,
            timeout=900,
        )
    assert not docker(
        "volume",
        "ls",
        "--quiet",
        "--filter",
        "label=org.praximesh.acceptance=" + token,
        capture_output=True,
    ).stdout.strip(), "Temporary database volume remains"
    print("Temporary PostgreSQL container and volume removed", flush=True)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
