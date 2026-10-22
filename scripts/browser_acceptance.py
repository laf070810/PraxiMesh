"""Run the real browser journey with isolated local state and owned processes."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--judge", action="store_true", help="Require real sandbox gates and worker"
    )
    options = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    artifacts = root / "test-results"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="praximesh-browser-") as temporary:
        env = dict(os.environ)
        env.update(
            {
                "PRAXIMESH_MODE": "local",
                "DATABASE_URL": "",
                "SQLITE_PATH": str(Path(temporary) / "app.sqlite3"),
                "APP_SECRET_KEY": secrets.token_urlsafe(48),
                "REGISTRATION_OPEN": "1",
                "APP_ALLOWED_HOSTS": "127.0.0.1,localhost",
                "JUDGE_ENABLED": "1" if options.judge else "0",
                "JUDGE_RECEIPT": str(Path(temporary) / "receipt.json"),
                "BROWSER_JUDGE": "1" if options.judge else "0",
            }
        )

        def run(*args, **kwargs):
            subprocess.run([sys.executable, *args], cwd=root, env=env, check=True, **kwargs)

        run("manage.py", "migrate", "--noinput", "--verbosity=0")
        run("manage.py", "content_import")
        if options.judge:
            run("manage.py", "sandbox_check")
        with socket.socket() as allocated:
            allocated.bind(("127.0.0.1", 0))
            port = allocated.getsockname()[1]
        env["BROWSER_URL"] = f"http://127.0.0.1:{port}"
        processes = []
        with (
            (artifacts / "browser-server.log").open("w") as server_log,
            (artifacts / "browser-worker.log").open("w") as worker_log,
        ):
            try:
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "manage.py",
                            "runserver",
                            f"127.0.0.1:{port}",
                            "--noreload",
                        ],
                        cwd=root,
                        env=env,
                        stdout=server_log,
                        stderr=subprocess.STDOUT,
                    )
                )
                if options.judge:
                    processes.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "manage.py",
                                "judge_worker",
                                "--worker-id=browser-acceptance",
                            ],
                            cwd=root,
                            env=env,
                            stdout=worker_log,
                            stderr=subprocess.STDOUT,
                        )
                    )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("Acceptance process exited; inspect test-results logs.")
                    try:
                        with opener.open(env["BROWSER_URL"] + "/health/", timeout=1) as response:
                            health = json.load(response)
                        if not options.judge or health["evaluation"] == "available":
                            break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Application or real judge did not become ready.")
                run("tests/browser_smoke.py", timeout=300)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


if __name__ == "__main__":
    main()
