"""Run the real browser journey with isolated local state and owned processes."""

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def bootstrap_moderator(run, environment):
    """Use a trusted management runner, returning only fresh browser credentials.

    ``run`` accepts management-script arguments and an ``env`` mapping. Container
    callers can adapt it to their owned control container with the DB owner role.
    No community writing or moderation state is created by this bootstrap.
    """
    credentials = {
        "BROWSER_MODERATOR_USERNAME": "community-reviewer-" + secrets.token_hex(6),
        "BROWSER_MODERATOR_PASSWORD": secrets.token_urlsafe(36),
    }
    private_environment = dict(environment, **credentials)
    run(
        "manage.py",
        "shell",
        "-c",
        "import os\nfrom django.contrib.auth import get_user_model\n"
        "from django.core.management import call_command\nfrom django.db import transaction\n"
        "with transaction.atomic():\n"
        "    user = get_user_model().objects.create_user("
        "username=os.environ['BROWSER_MODERATOR_USERNAME'], "
        "password=os.environ['BROWSER_MODERATOR_PASSWORD'])\n"
        "    call_command('community_moderator', user.username, grant=True)\n"
        "    assert not user.is_staff and not user.is_superuser and not user.groups.exists()\n"
        "    assert set(user.user_permissions.values_list('content_type__app_label', 'codename')) "
        "== {('community', 'moderate_content')}\n",
        env=private_environment,
    )
    return credentials


def cleanup_moderator(run, environment, credentials):
    """Delete only the fixture proved by its exact name, random secret and role.

    Call only after bootstrap returned successfully. A browser may already have
    deleted it; a replaced or modified account fails closed instead of being removed.
    """
    if (
        set(credentials) != {"BROWSER_MODERATOR_USERNAME", "BROWSER_MODERATOR_PASSWORD"}
        or not isinstance(credentials["BROWSER_MODERATOR_USERNAME"], str)
        or not re.fullmatch(
            r"community-reviewer-[a-f0-9]{12}", credentials["BROWSER_MODERATOR_USERNAME"]
        )
        or not isinstance(credentials["BROWSER_MODERATOR_PASSWORD"], str)
        or len(credentials["BROWSER_MODERATOR_PASSWORD"]) < 32
    ):
        raise ValueError("Expected this run's synthetic reviewer credentials")
    run(
        "manage.py",
        "shell",
        "-c",
        "import os\nfrom django.contrib.auth import get_user_model\n"
        "from django.core.management.base import CommandError\nfrom django.db import transaction\n"
        "with transaction.atomic():\n"
        "    user=get_user_model().objects.select_for_update().filter("
        "username=os.environ['BROWSER_MODERATOR_USERNAME']).first()\n"
        "    if user is not None:\n"
        "        if (not user.check_password(os.environ['BROWSER_MODERATOR_PASSWORD']) "
        "or user.is_staff or user.is_superuser or user.groups.exists() "
        "or set(user.user_permissions.values_list('content_type__app_label','codename')) "
        "!= {('community','moderate_content')}):\n"
        "            raise CommandError('Synthetic reviewer ownership check failed')\n"
        "        user.delete()\n",
        env=dict(environment, **credentials),
    )


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
        # This launcher owns a new local HTTP endpoint, not an inherited TLS fixture.
        env.pop("BROWSER_TEST_SPKI", None)
        env.pop("BROWSER_CA_FILE", None)
        env.pop("BROWSER_MODERATOR_USERNAME", None)
        env.pop("BROWSER_MODERATOR_PASSWORD", None)

        def run(*args, **kwargs):
            environment = kwargs.pop("env", env)
            subprocess.run([sys.executable, *args], cwd=root, env=environment, check=True, **kwargs)

        run("manage.py", "migrate", "--noinput", "--verbosity=0")
        run("manage.py", "content_import", "--translations", str(root / "content/en.json"))
        run("manage.py", "project_import", str(root / "content/projects.json"))
        browser_env = dict(env)
        if options.judge:
            run("manage.py", "sandbox_check")
        with socket.socket() as allocated:
            allocated.bind(("127.0.0.1", 0))
            port = allocated.getsockname()[1]
        env["BROWSER_URL"] = f"http://127.0.0.1:{port}"
        browser_env["BROWSER_URL"] = env["BROWSER_URL"]
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
                credentials = bootstrap_moderator(run, env)
                try:
                    run(
                        "tests/browser_community.py",
                        env=dict(browser_env, **credentials),
                        timeout=300,
                    )
                finally:
                    cleanup_moderator(run, env, credentials)
                run("tests/browser_i18n.py", timeout=300)
                run("tests/browser_projects_i18n.py", timeout=300)
                if options.judge:
                    run("tests/browser_practice.py", timeout=300)
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
