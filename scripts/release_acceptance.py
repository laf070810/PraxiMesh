"""Import a verified release and exercise its real Web/worker pair in containers.

Only synthetic data and owned, temporary Docker resources are used. The app's
HTTP port is loopback-only. This validates the packaged core; public TLS and
deployment activation/rollback remain separate checks.
"""

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.production_acceptance import POSTGRES, Client, database_roles  # noqa: E402
from scripts.release import docker, initial_content_commands, inspect_images, verify  # noqa: E402


def runtime_args(name, network, memory, pids):
    return [
        "--name",
        name,
        "--network",
        network,
        "--init",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        f"--memory={memory}",
        f"--pids-limit={pids}",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=128m,uid=65532,gid=65532,mode=700",
    ]


def start_container(stack, name, arguments, environment=None):
    stack.callback(remove_container, name)
    output = docker("run", "--detach", *arguments, env=environment, capture_output=True)
    return output.stdout.decode().strip()


def remove_container(name):
    existing = docker(
        "container", "ls", "--all", "--quiet", "--filter", f"name=^{name}$", capture_output=True
    )
    if existing.stdout.strip():
        docker("rm", "--force", "--volumes", name, capture_output=True)


def env_flags(environment):
    return [part for name in environment for part in ["--env", name]]


def browser_environment(environment, base):
    result = dict(environment, BROWSER_URL=base, BROWSER_JUDGE="1")
    for key in list(result):
        if key in {"BROWSER_TEST_SPKI", "BROWSER_CA_FILE"} or key.startswith("BROWSER_COACHING"):
            del result[key]
    # This isolated release's Web configuration does not enable a model provider.
    result["BROWSER_COACHING"] = "0"
    return result


def check_admission(base, control):
    """Real tasks drain while reading/drafts remain usable; no injected verdicts."""
    client = Client(base, None)
    username, password = "drain-" + uuid.uuid4().hex[:12], secrets.token_urlsafe(32)
    assert client.request("/accounts/register/")[0] == 200
    assert (
        client.request(
            "/accounts/register/",
            {"username": username, "password1": password, "password2": password},
        )[0]
        == 200
    )
    client.login(username, password)
    source = "def solve(values):\n    return sum(x for x in values if x > 0)\n"
    delayed = "import time\ndef solve(values):\n    time.sleep(1)\n    return sum(x for x in values if x > 0)\n"
    status, first = client.submit(delayed)
    assert status == 202
    token = str(uuid.uuid4())
    timed_out = control("drain", token, timeout=0, success=False)
    assert b"Admission remains paused" in timed_out.stderr
    state = json.loads(timed_out.stdout)
    assert state["admission"] == "paused" and state["queued"] + state["running"] == 1
    assert client.submit(source)[0] == 503
    assert client.request("/learn/")[0] == 200
    assert client.request("/problems/sum-positive/")[0] == 200
    assert (
        client.request(
            "/problems/sum-positive/draft/",
            {"source": source, "version": client.version},
            {"Accept": "application/json"},
        )[0]
        == 200
    )
    drained = json.loads(control("drain", token, timeout=60).stdout)
    assert drained["queued"] == drained["running"] == 0
    assert drained["admission"] == "paused"
    assert (
        json.loads(client.request(f"/api/submissions/{first['id']}/")[2])["verdict"] == "accepted"
    )
    assert client.submit(source)[0] == 503
    control("resume", str(uuid.uuid4()), success=False)
    assert json.loads(control("status").stdout)["admission"] == "paused"
    control("resume", token)
    status, second = client.submit(source)
    assert status == 202
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = json.loads(client.request(f"/api/submissions/{second['id']}/")[2])
        if result["status"] == "finished":
            assert result["verdict"] == "accepted"
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("Real submission did not finish after admission resumed")
    assert (
        client.request("/accounts/delete/", {"password": password, "confirm": "DELETE"})[0] == 200
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    options = parser.parse_args()
    manifest = verify(options.release, options.sha256)
    docker("image", "load", "--input", str(options.release / "images.tar"), capture_output=True)
    inspect_images(manifest)
    artifacts = ROOT / "test-results"
    artifacts.mkdir(exist_ok=True)
    output_path = artifacts / "release-acceptance.json"
    output_path.unlink(missing_ok=True)
    token = uuid.uuid4().hex[:12]
    network, pg, web, worker = [
        f"praximesh-release-{token}-{role}" for role in ["net", "pg", "web", "worker"]
    ]
    ingress = network + "-front"
    data_volume = f"praximesh-release-{token}-data"
    print("Starting the imported Web/WASI release on a private Docker network", flush=True)
    with ExitStack() as stack:
        docker("network", "create", "--internal", network, capture_output=True)
        stack.callback(docker, "network", "rm", network, capture_output=True)
        # Published loopback ports use a separate bridge. The worker is never
        # attached to it; its controller can reach only the internal database.
        docker("network", "create", ingress, capture_output=True)
        stack.callback(docker, "network", "rm", ingress, capture_output=True)
        docker(
            "volume",
            "create",
            "--label",
            "org.praximesh.acceptance=" + token,
            data_volume,
            capture_output=True,
        )
        stack.callback(docker, "volume", "rm", data_volume, capture_output=True)
        owner_password, web_password, worker_password = [
            secrets.token_urlsafe(32) for _ in range(3)
        ]
        pg_env = dict(os.environ, POSTGRES_PASSWORD=owner_password)
        start_container(
            stack,
            pg,
            [
                "--name",
                pg,
                "--network",
                ingress,
                "--memory=512m",
                "--pids-limit=128",
                "--mount",
                f"type=volume,src={data_volume},dst=/var/lib/postgresql/data",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_DB=praximesh_test",
                POSTGRES,
            ],
            pg_env,
        )
        docker("network", "connect", network, pg, capture_output=True)
        db_port = int(
            docker("port", pg, "5432/tcp", capture_output=True)
            .stdout.decode()
            .strip()
            .rsplit(":", 1)[1]
        )

        def url(user, password, host):
            return f"postgresql://{user}:{password}@{host}/praximesh_test?sslmode=disable"

        owner_url = url("postgres", owner_password, f"127.0.0.1:{db_port}")
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(owner_url, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        common = {
            "PRAXIMESH_MODE": "local",
            "DB_SSLMODE": "disable",
            "APP_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "JUDGE_ENABLED": "1",
            "REGISTRATION_OPEN": "1",
        }
        owner_config = dict(
            common,
            DATABASE_URL=url("postgres", owner_password, pg),
            APP_SECRET_KEY=secrets.token_urlsafe(48),
        )
        for command in [["migrate", "--noinput"], *initial_content_commands(manifest)]:
            name = f"praximesh-release-{token}-prepare"
            stack.callback(remove_container, name)
            docker(
                "run",
                "--rm",
                *runtime_args(name, network, "512m", 64),
                *env_flags(owner_config),
                manifest["images"]["web"],
                "python",
                "manage.py",
                *command,
                env=dict(os.environ, **owner_config),
                capture_output=True,
            )
        database_roles(owner_url, web_password, worker_password)
        web_config = dict(
            common,
            DATABASE_URL=url("acceptance_web", web_password, pg),
            APP_SECRET_KEY=secrets.token_urlsafe(48),
        )
        worker_config = dict(
            common,
            DATABASE_URL=url("acceptance_judge", worker_password, pg),
            APP_SECRET_KEY=secrets.token_urlsafe(48),
        )
        start_container(
            stack,
            web,
            [
                *runtime_args(web, ingress, "512m", 64),
                "--publish",
                "127.0.0.1::8000",
                *env_flags(web_config),
                manifest["images"]["web"],
            ],
            dict(os.environ, **web_config),
        )
        start_container(
            stack,
            worker,
            [
                *runtime_args(worker, network, "2g", 32),
                *env_flags(worker_config),
                manifest["images"]["worker"],
            ],
            dict(os.environ, **worker_config),
        )

        def save_logs():
            for name, role in [(web, "web"), (worker, "worker")]:
                result = docker("logs", name, capture_output=True)
                (artifacts / f"release-{role}.log").write_bytes(result.stdout + result.stderr)

        # Capture before container cleanup, including on a failed assertion.
        stack.callback(save_logs)
        web_port = int(
            docker("port", web, "8000/tcp", capture_output=True)
            .stdout.decode()
            .strip()
            .rsplit(":", 1)[1]
        )
        base = f"http://127.0.0.1:{web_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            for name in [web, worker]:
                running = docker(
                    "inspect", "--format", "{{.State.Running}}", name, capture_output=True
                ).stdout.strip()
                if running != b"true":
                    raise RuntimeError("Release container stopped before readiness")
            try:
                with opener.open(base + "/health/", timeout=2) as response:
                    health = json.load(response)
                if health["evaluation"] == "available":
                    break
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Packaged worker did not pass its real startup probes")
        print(
            "Real startup probes passed; checking image boundaries and browser submissions",
            flush=True,
        )
        for name, role in [(web, "web"), (worker, "worker")]:
            info = json.loads(docker("inspect", name, capture_output=True).stdout)[0]
            assert info["Config"]["User"] == "65532:65532"
            assert info["HostConfig"]["ReadonlyRootfs"]
            assert "ALL" in info["HostConfig"]["CapDrop"]
            assert not info["HostConfig"].get("Binds")
            if role == "worker":
                assert set(info["NetworkSettings"]["Networks"]) == {network}
            check = (
                "import importlib.util, os; from pathlib import Path; from judge.sandbox import policy_digest; "
                "assert os.getuid()==65532; assert not Path('/var/run/docker.sock').exists(); "
                f"assert (importlib.util.find_spec('wasmtime') is not None)=={role == 'worker'}; "
                f"assert Path('/opt/praximesh/wasi').exists()=={role == 'worker'}; "
                "print(policy_digest())"
            )
            digest = (
                docker("exec", name, "python", "-c", check, capture_output=True)
                .stdout.decode()
                .strip()
            )
            assert digest == manifest["judge"]["policy_sha256"]
        receipt_summary = json.loads(
            docker(
                "exec",
                worker,
                "python",
                "-c",
                "import os,json; from pathlib import Path; "
                "os.environ.setdefault('DJANGO_SETTINGS_MODULE','praximesh.settings'); "
                "import django; django.setup(); from django.conf import settings; "
                "from judge.backends import configured_sandbox; "
                "from core.management.commands.sandbox_check import check_receipt; "
                "identity=configured_sandbox().preflight(); check_receipt(identity); "
                "receipt=json.loads(Path(settings.JUDGE_RECEIPT).read_text()); "
                "print(json.dumps({'count':len(receipt['passed']),'suite':receipt['suite']}))",
                capture_output=True,
            ).stdout
        )
        browser_env = browser_environment(os.environ, base)
        with (artifacts / "release-browser.log").open("wb") as log:
            subprocess.run(
                [sys.executable, "tests/browser_smoke.py"],
                cwd=ROOT,
                env=browser_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=240,
            )
        feature_browsers = {
            "localization/migrations/0001_initial.py": "browser_i18n",
            "projects/migrations/0001_initial.py": "browser_projects_i18n",
            "practice_api/migrations/0001_initial.py": "browser_practice",
        }
        checked_feature_browsers = []
        for capability, script in feature_browsers.items():
            if capability not in manifest["source_files"]:
                continue
            if (
                script == "browser_practice"
                and browser_env.get("JUDGE_WASI_SHA256") != manifest["judge"]["bundle_sha256"]
            ):
                raise RuntimeError(
                    "CLI acceptance requires the matching verified local WASI bundle"
                )
            with (artifacts / f"release-{script}.log").open("wb") as log:
                subprocess.run(
                    [sys.executable, f"tests/{script}.py"],
                    cwd=ROOT,
                    env=browser_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=300,
                )
            checked_feature_browsers.append(script)

        advanced = None
        if "coaching/migrations/0001_initial.py" in manifest["source_files"]:
            from scripts.advanced_browsers import run_advanced_browsers

            def owner_run(*arguments, env=None):
                assert arguments[0] == "manage.py"
                name = "praximesh-release-" + uuid.uuid4().hex[:12] + "-fixture"
                stack.callback(remove_container, name)
                fixture_env = dict(owner_config)
                fixture_env.update(
                    {key: value for key, value in (env or {}).items() if key.startswith("BROWSER_")}
                )
                return docker(
                    "run",
                    "--rm",
                    *runtime_args(name, network, "512m", 64),
                    *env_flags(fixture_env),
                    manifest["images"]["web"],
                    "python",
                    *arguments,
                    env=dict(os.environ, **fixture_env),
                    capture_output=True,
                ).stdout

            advanced = run_advanced_browsers(
                browser_env,
                owner_run,
                artifacts / "release-advanced",
                include_challenges="challenges/migrations/0001_initial.py"
                in manifest["source_files"],
                include_object_api="judge/object_api.py" in manifest["source_files"],
            )

        def control(action, token=None, timeout=180, success=True):
            name = "praximesh-release-" + uuid.uuid4().hex[:12] + "-control"
            stack.callback(remove_container, name)
            arguments = ["judge_admission", action, "--timeout", str(timeout)]
            if token:
                arguments += ["--token", token]
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *runtime_args(name, network, "512m", 64),
                    *env_flags(owner_config),
                    manifest["images"]["web"],
                    "python",
                    "manage.py",
                    *arguments,
                ],
                env=dict(os.environ, **owner_config),
                capture_output=True,
                timeout=timeout + 30,
            )
            assert (result.returncode == 0) == success, result.stderr.decode()
            return result

        print("Checking admission pause, real queue drain and recovery", flush=True)
        check_admission(base, control)
        with psycopg.connect(owner_url) as connection:
            assert connection.execute("SELECT count(*) FROM auth_user").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM core_submission").fetchone()[0] == 0
        # Force a genuinely invalid runtime selection in a new container. It must
        # stop before probing or connecting to any real application database.
        reject_name = f"praximesh-release-{token}-reject"
        stack.callback(remove_container, reject_name)
        rejected = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                *runtime_args(reject_name, "none", "2g", 32),
                "--env",
                "JUDGE_ENABLED=1",
                "--env",
                "JUDGE_WASI_SHA256=" + "0" * 64,
                manifest["images"]["worker"],
            ],
            capture_output=True,
            timeout=30,
        )
        assert rejected.returncode != 0 and b"WASI artifact verification failed" in rejected.stderr
        result = {
            "passed": True,
            "release_sha256": options.sha256,
            "source_id": manifest["source_id"],
            "images": manifest["images"],
            "wasi_manifest_sha256": manifest["judge"]["bundle_sha256"],
            "scope": "private Docker network, loopback HTTP, synthetic database",
            "web_and_worker_nonroot_readonly": True,
            "web_has_no_wasi_runtime": True,
            "no_docker_socket_or_host_binds": True,
            "worker_network_internal": True,
            "startup_probes": receipt_summary["count"],
            "startup_probe_suite": receipt_summary["suite"],
            "feature_browser_journeys": checked_feature_browsers,
            "advanced_browser_journeys": advanced,
            "browser_real_run_wrong_accepted_progress_delete": True,
            "admission_timeout_closed_real_drain_and_resume": True,
            "wrong_runtime_stops_before_worker": True,
            "database": "PostgreSQL with separate owner/Web/worker roles",
        }
    result["cleanup_passed"] = True
    remaining = docker(
        "volume",
        "ls",
        "--quiet",
        "--filter",
        "label=org.praximesh.acceptance=" + token,
        capture_output=True,
    )
    assert not remaining.stdout.strip(), "Temporary database volume remains"
    result["database_volume_removed"] = True
    output_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
