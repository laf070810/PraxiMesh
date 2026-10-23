"""Exercise a disposable loopback TLS deployment with PostgreSQL and real WASI.

Requires Docker, OpenSSL, the pinned Caddy executable, browser/judge dependencies,
Chromium and an explicitly configured verified WASI bundle. No system trust or
shared service configuration is changed. Credentials and private keys are temporary.
"""

import argparse
import base64
import hashlib
import http.client
import http.cookiejar
import json
import math
import os
import platform
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.browser_acceptance import bootstrap_moderator, cleanup_moderator  # noqa: E402
from scripts.deploy_database import (  # noqa: E402
    WORKER_PRIVILEGES,
    web_read_tables,
    web_write_tables,
)

POSTGRES = (
    "postgres:17-alpine@sha256:e4cb93687b062cd25a0e8f672d85a7708235009d70d31f8a7698d9370092a9c4"
)
READ_SAMPLES = 120
READ_CONCURRENCY = 8
JOB_SAMPLES = 6


def run(*command, env=None, timeout=120, **kwargs):
    try:
        return subprocess.run(
            command, cwd=ROOT, env=env, check=True, capture_output=True, timeout=timeout, **kwargs
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # These commands never place credentials in argv or normal output.
        (ROOT / "test-results").mkdir(exist_ok=True)
        (ROOT / "test-results/production-command-failure.log").write_bytes(
            (error.stdout or b"") + (error.stderr or b"")
        )
        raise


def port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def start(stack, command, env, log):
    stream = stack.enter_context(log.open("w"))
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stack.callback(stop, process)
    return process


def pki(directory):
    ca, ca_key = directory / "ca.pem", directory / "ca.key"
    cert, key = directory / "server.pem", directory / "server.key"
    request = directory / "server.csr"
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
    )
    for output in [ca_key, key]:
        run(
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(output),
        )
        output.chmod(0o600)
    run(
        "openssl",
        "req",
        "-new",
        "-x509",
        "-key",
        str(ca_key),
        "-out",
        str(ca),
        "-days",
        "1",
        "-subj",
        "/CN=PraxiMesh ephemeral acceptance CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign",
    )
    run("openssl", "req", "-new", "-key", str(key), "-out", str(request), "-subj", "/CN=localhost")
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(request),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "1",
        "-extfile",
        str(extensions),
        "-out",
        str(cert),
    )
    public_key = run("openssl", "x509", "-in", str(cert), "-pubkey", "-noout")
    der = run("openssl", "pkey", "-pubin", "-outform", "DER", input=public_key)
    pin = base64.b64encode(hashlib.sha256(der).digest()).decode()
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return cert, key, pin, context


class SourceHTTPSHandler(urllib.request.HTTPSHandler):
    """Bind actual loopback clients without trusting a caller-supplied IP header."""

    def __init__(self, context, address):
        super().__init__(context=context)
        self.address = address

    def https_open(self, request):
        return self.do_open(
            partial(http.client.HTTPSConnection, source_address=(self.address, 0)),
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class Client:
    def __init__(self, base, context, *, source_address=None):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            SourceHTTPSHandler(context, source_address)
            if source_address
            else urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def request(self, path, data=None, headers=None):
        headers = dict(headers or {})
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if data is not None:
            headers.setdefault("Origin", self.base)
            headers.setdefault("Referer", self.base + path)
            token = next(
                (cookie.value for cookie in self.cookies if cookie.name == "csrftoken"), ""
            )
            headers.setdefault("X-CSRFToken", token)
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def login(self, username, password):
        assert self.request("/accounts/login/")[0] == 200
        assert (
            self.request("/accounts/login/", {"username": username, "password": password})[0] == 200
        )
        assert any(cookie.name == "sessionid" for cookie in self.cookies)
        status, _, body = self.request("/problems/sum-positive/")
        assert status == 200
        self.version = re.search(rb'name="version" value="([0-9]+)"', body).group(1).decode()

    def submit(self, source):
        status, _, body = self.request(
            "/problems/sum-positive/submit/",
            {"source": source, "version": self.version, "mode": "submit"},
            {"Accept": "application/json"},
        )
        return status, json.loads(body)


def percentiles(values):
    ordered = sorted(values)
    return {f"p{p}": round(ordered[math.ceil(p * len(ordered) / 100) - 1], 4) for p in [50, 95, 99]}


def database_roles(owner_url, web_password, worker_password):
    with psycopg.connect(owner_url, autocommit=True) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        for name, password in [
            ("acceptance_web", web_password),
            ("acceptance_judge", worker_password),
        ]:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(password)
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE praximesh_test TO {}").format(
                    sql.Identifier(name)
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name))
            )
        connection.execute(
            sql.SQL("GRANT SELECT ON {} TO acceptance_web").format(
                sql.SQL(", ").join(map(sql.Identifier, web_read_tables(connection)))
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO acceptance_web").format(
                sql.SQL(", ").join(map(sql.Identifier, web_write_tables(connection)))
            )
        )
        connection.execute(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO acceptance_web"
        )
        for table, privileges in WORKER_PRIVILEGES.items():
            connection.execute(
                sql.SQL("GRANT {} ON {} TO acceptance_judge").format(
                    sql.SQL(privileges), sql.Identifier(table)
                )
            )


def denied(url, statement):
    with psycopg.connect(url, autocommit=True) as connection:
        try:
            connection.execute(statement)
        except psycopg.errors.InsufficientPrivilege:
            return
    raise AssertionError("Database role exceeded its privileges")


def transport_checks(base, http_port, backend_port, context):
    client = Client(base, context)
    for path in ["/", "/practice/", "/static/app.css", "/static/app.js"]:
        status, headers, _ = client.request(path)
        assert status == 200
        assert "max-age=31536000" in headers["Strict-Transport-Security"]
        assert headers["X-Content-Type-Options"] == "nosniff"
    status, headers, _ = client.request(
        "/", headers={"X-Forwarded-Proto": "http,https", "X-Real-IP": "192.0.2.1"}
    )
    assert status == 200 and "max-age=" in headers["Strict-Transport-Security"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    with closing(http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)) as connection:
        connection.request("GET", "/practice/?q=test")
        response = connection.getresponse()
        assert (
            response.status in {301, 308}
            and response.getheader("Location") == base + "/practice/?q=test"
        )
        response.read()
    with closing(
        http.client.HTTPConnection(
            "127.0.0.1", backend_port, timeout=5, source_address=("127.0.0.2", 0)
        )
    ) as connection:
        connection.request(
            "GET",
            "/health/",
            headers={
                "Host": urllib.parse.urlsplit(base).netloc,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-SSL": "on",
            },
        )
        response = connection.getresponse()
        assert response.status == 301
        response.read()
    tls_port = urllib.parse.urlsplit(base).port
    with socket.create_connection(("127.0.0.1", tls_port), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname="localhost") as secure:
            protocol = secure.version()
            assert protocol in {"TLSv1.2", "TLSv1.3"}
    rejections = {}
    for rejected_context, hostname, expected_error in [
        (ssl.create_default_context(), "localhost", ssl.SSLCertVerificationError),
        (context, "wrong.example.invalid", ssl.SSLError),
    ]:
        try:
            with socket.create_connection(("127.0.0.1", tls_port), timeout=5) as connection:
                with rejected_context.wrap_socket(connection, server_hostname=hostname):
                    pass
        except expected_error as error:
            rejections[hostname] = type(error).__name__
        else:
            raise AssertionError("Invalid TLS peer unexpectedly accepted")
    status, _, body = client.request("/", headers={"Host": "wrong.example.invalid"})
    # An unmatched Caddy site can return an empty 200 without contacting upstream.
    assert status in {400, 404, 421} or (status == 200 and not body)
    with closing(http.client.HTTPConnection("127.0.0.1", backend_port, timeout=5)) as connection:
        connection.request(
            "GET", "/", headers={"Host": "wrong.example.invalid", "X-Forwarded-Proto": "https"}
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    assert client.request("/accounts/login/", {"padding": "x" * 110000})[0] == 413
    status, _, body = client.request("/missing-acceptance-page/")
    assert status == 404 and b"Traceback" not in body and b"DEBUG" not in body
    return {
        "tls_protocol": protocol,
        "verified_private_ca": True,
        "hostname_check_enabled": context.check_hostname,
        "negative_tls_errors": rejections,
        "invalid_tls_rejected": True,
        "http_redirect": True,
        "forwarded_headers": True,
        "request_body_limit": True,
        "production_404": True,
    }


def browse(base, context):
    paths = ["/", "/practice/", "/learn/python-to-numerical/"]

    def one(index):
        begin = time.monotonic()
        assert Client(base, context).request(paths[index % len(paths)])[0] == 200
        return time.monotonic() - begin

    begin = time.monotonic()
    with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as pool:
        timings = list(pool.map(one, range(READ_SAMPLES)))
    duration = time.monotonic() - begin
    summary = {
        "requests": READ_SAMPLES,
        "clients": READ_CONCURRENCY,
        "errors": 0,
        "seconds": round(duration, 3),
        "requests_per_second": round(READ_SAMPLES / duration, 2),
        "latency_seconds": percentiles(timings),
    }
    assert summary["latency_seconds"]["p95"] < 1, summary
    return summary


def load_jobs(base, context, password, owner_url):
    # Browser journeys share 127.0.0.1. Use another real client address for the
    # load cohort so account setup does not exhaust that cohort's existing quota.
    clients = [Client(base, context, source_address="127.0.0.2") for _ in range(JOB_SAMPLES)]
    for index, client in enumerate(clients):
        client.login(f"acceptance-load-{index}", password)
    source = "def solve(values):\n    return sum(x for x in values if x > 0)\n"
    begin = time.monotonic()
    with ThreadPoolExecutor(max_workers=JOB_SAMPLES) as pool:
        submitted = list(pool.map(lambda client: client.submit(source), clients))
    assert all(status == 202 for status, _ in submitted), [status for status, _ in submitted]
    jobs = [body["id"] for _, body in submitted]
    assert clients[0].request(f"/api/submissions/{jobs[1]}/")[0] == 404
    # Browse while the independent worker executes the real queue.
    during_judge = browse(base, context)
    pending = set(range(JOB_SAMPLES))
    deadline = begin + 60
    while pending and time.monotonic() < deadline:
        for index in list(pending):
            status, _, body = clients[index].request(f"/api/submissions/{jobs[index]}/")
            result = json.loads(body)
            assert status == 200
            if result["status"] == "finished":
                assert result["verdict"] == "accepted", result
                pending.remove(index)
        if pending:
            time.sleep(0.5)
    assert not pending, "Six real submissions exceeded the local 60-second regression target"
    with psycopg.connect(owner_url) as connection:
        rows = connection.execute(
            "SELECT EXTRACT(EPOCH FROM started_at-created_at), EXTRACT(EPOCH FROM finished_at-started_at), "
            "EXTRACT(EPOCH FROM finished_at-created_at) FROM core_submission WHERE id = ANY(%s)",
            ([uuid.UUID(job) for job in jobs],),
        ).fetchall()
    assert len(rows) == JOB_SAMPLES
    summary = {
        "submissions": JOB_SAMPLES,
        "accounts": JOB_SAMPLES,
        "concurrent_burst": JOB_SAMPLES,
        "accepted": JOB_SAMPLES,
        "workers": 1,
        "case_count_each": next(
            len(problem["tests"])
            for problem in json.loads((ROOT / "content/starter.json").read_text())["problems"]
            if problem["id"] == "sum-positive"
        ),
        "queue_seconds": percentiles([float(row[0]) for row in rows]),
        "execution_seconds": percentiles([float(row[1]) for row in rows]),
        "total_seconds": percentiles([float(row[2]) for row in rows]),
        "cross_account_result_denied": True,
    }
    return summary, during_judge, clients[0], source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", type=Path, required=True)
    options = parser.parse_args()
    if os.environ.get("JUDGE_BACKEND") != "wasi" or not os.environ.get("JUDGE_WASI_SHA256"):
        parser.error(
            "Set JUDGE_BACKEND=wasi and explicit JUDGE_WASI_BUNDLE / JUDGE_WASI_SHA256 first"
        )
    caddy = options.caddy.resolve()
    caddy_version = run(str(caddy), "version").decode().strip()
    if not caddy_version.startswith("v2.11.7 "):
        parser.error("Use scripts/fetch_caddy.py to fetch the pinned acceptance version")
    artifacts = ROOT / "test-results"
    artifacts.mkdir(exist_ok=True)
    result_path = artifacts / "production-acceptance.json"
    result_path.unlink(missing_ok=True)
    container = "praximesh-production-" + uuid.uuid4().hex[:12]
    with ExitStack() as stack:
        temporary = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="praximesh-production-"))
        )
        cert, key, pin, context = pki(temporary)
        tls_port, http_port, backend_port = port(), port(), port()
        base = f"https://127.0.0.1:{tls_port}"
        owner_password, web_password, worker_password = [
            secrets.token_urlsafe(32) for _ in range(3)
        ]
        password = secrets.token_urlsafe(32)
        env = dict(os.environ, POSTGRES_PASSWORD=owner_password)
        for name in list(env):
            if name.startswith("COACHING_") or name.startswith("BROWSER_COACHING"):
                del env[name]
        print("Starting disposable PostgreSQL and preparing production settings", flush=True)
        stack.callback(lambda: run("docker", "rm", "-f", "--volumes", container))
        run(
            "docker",
            "run",
            "--pull=missing",
            "--rm",
            "--detach",
            "--name",
            container,
            "--memory=512m",
            "--pids-limit=128",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_DB=praximesh_test",
            POSTGRES,
            env=env,
        )
        db_port = int(
            run("docker", "port", container, "5432/tcp").decode().strip().rsplit(":", 1)[1]
        )
        del env["POSTGRES_PASSWORD"]

        def database_url(user, credential):
            return f"postgresql://{user}:{credential}@127.0.0.1:{db_port}/praximesh_test?sslmode=disable"

        owner_url = database_url("postgres", owner_password)
        web_url = database_url("acceptance_web", web_password)
        worker_url = database_url("acceptance_judge", worker_password)
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(owner_url, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        env.update(
            {
                "PRAXIMESH_MODE": "production",
                "APP_SECRET_KEY": secrets.token_urlsafe(48),
                "APP_ALLOWED_HOSTS": "127.0.0.1,localhost",
                "DATABASE_URL": owner_url,
                "DB_SSLMODE": "disable",
                "TRUST_TLS_PROXY": "1",
                "TRUSTED_PROXY_IPS": "127.0.0.1",
                "CSRF_TRUSTED_ORIGINS": base,
                "APP_STATIC_ROOT": str(temporary / "static"),
                "REGISTRATION_OPEN": "1",
                "JUDGE_ENABLED": "1",
                "JUDGE_RECEIPT": str(temporary / "receipt.json"),
                "BROWSER_URL": base,
                "BROWSER_TEST_SPKI": pin,
                "BROWSER_JUDGE": "1",
                "COACHING_ENABLED": "0",
                "ACCEPTANCE_PASSWORD": password,
                "XDG_DATA_HOME": str(temporary / "data"),
                "XDG_CONFIG_HOME": str(temporary / "config"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        for command in [
            ["migrate", "--noinput"],
            ["content_import", "--translations", str(ROOT / "content/en.json")],
            ["project_import", str(ROOT / "content/projects.json")],
            ["challenge_import", str(ROOT / "content/challenges.json")],
            ["collectstatic", "--noinput"],
            ["check", "--deploy", "--fail-level", "WARNING"],
        ]:
            run(sys.executable, "manage.py", *command, env=env)
        run(
            sys.executable,
            "manage.py",
            "shell",
            "-c",
            "import os; from django.contrib.auth import get_user_model; "
            "from django.contrib.auth.hashers import make_password; "
            "password = make_password(os.environ['ACCEPTANCE_PASSWORD']); "
            f"get_user_model().objects.bulk_create([get_user_model()(username=f'acceptance-load-{{i}}', password=password) for i in range({JOB_SAMPLES})])",
            env=env,
        )
        del env["ACCEPTANCE_PASSWORD"]
        moderator_credentials = bootstrap_moderator(
            lambda *arguments, env: run(sys.executable, *arguments, env=env), env
        )
        stack.callback(
            cleanup_moderator,
            lambda *arguments, env: run(sys.executable, *arguments, env=env),
            env,
            moderator_credentials,
        )
        database_roles(owner_url, web_password, worker_password)
        denied(web_url, "CREATE TABLE forbidden_acceptance_table (id integer)")
        denied(web_url, "UPDATE core_problemversion SET data=data WHERE false")
        denied(web_url, "UPDATE core_workerhealth SET ready=true WHERE false")
        denied(worker_url, "SELECT * FROM auth_user LIMIT 1")
        denied(worker_url, "SELECT * FROM learning_note LIMIT 1")
        denied(worker_url, "SELECT * FROM community_report LIMIT 1")
        denied(worker_url, "SELECT * FROM practice_api_accesstoken LIMIT 1")
        denied(worker_url, "SELECT * FROM showcase_sharedresult LIMIT 1")
        denied(worker_url, "SELECT * FROM localization_problemtranslation LIMIT 1")
        denied(worker_url, "SELECT * FROM projects_projectstage LIMIT 1")
        denied(
            web_url, "UPDATE localization_problemtranslation SET published=published WHERE false"
        )
        denied(web_url, "UPDATE projects_project SET published=published WHERE false")
        for table in ("organizations_managedobject", "organizations_publicationlock"):
            denied(web_url, f"DELETE FROM {table} WHERE false")
        for table in (
            "challenges_challenge",
            "challenges_challengerevision",
            "challenges_challengetask",
        ):
            denied(web_url, f"DELETE FROM {table} WHERE false")
            denied(web_url, f"UPDATE {table} SET id=id WHERE false")
            denied(worker_url, f"SELECT * FROM {table} LIMIT 0")
        denied(worker_url, "SELECT * FROM challenges_participation LIMIT 0")
        for table in (
            "organizations_organization",
            "organizations_membership",
            "organizations_invitation",
            "organizations_course",
            "organizations_courserevision",
            "organizations_courseunit",
            "organizations_courseenrollment",
            "organizations_package",
            "organizations_packagerevision",
            "organizations_organizationevent",
            "organizations_managedobject",
            "organizations_publicationlock",
            "coaching_learningplan",
            "coaching_hintrequest",
            "coaching_dailyusage",
            "coaching_slotlock",
            "coaching_executionlease",
        ):
            denied(worker_url, f"SELECT * FROM {table} LIMIT 0")
        web_env = dict(env, DATABASE_URL=web_url)
        worker_env = dict(env, DATABASE_URL=worker_url, APP_SECRET_KEY=secrets.token_urlsafe(48))
        proxy_env = dict(
            os.environ, XDG_DATA_HOME=env["XDG_DATA_HOME"], XDG_CONFIG_HOME=env["XDG_CONFIG_HOME"]
        )
        print("Running the real sandbox probes with the restricted worker role", flush=True)
        with (artifacts / "production-sandbox.log").open("wb") as log:
            log.write(
                run(sys.executable, "manage.py", "sandbox_check", env=worker_env, timeout=300)
            )
        config = temporary / "Caddyfile"
        config.write_text(
            "{\n admin off\n auto_https disable_redirects\n skip_install_trust\n}\n"
            f"{base} {{\n bind 127.0.0.1\n tls {cert} {key}\n"
            f" import {ROOT / 'deploy/proxy.caddy'} 127.0.0.1:{backend_port}\n}}\n"
            f"http://127.0.0.1:{http_port} {{\n bind 127.0.0.1\n redir {base}{{uri}} permanent\n}}\n"
        )
        run(
            str(caddy), "validate", "--config", str(config), "--adapter", "caddyfile", env=proxy_env
        )
        web = start(
            stack,
            [
                sys.executable,
                "-m",
                "gunicorn",
                "praximesh.wsgi:application",
                "--config",
                "gunicorn.conf.py",
                "--bind",
                f"127.0.0.1:{backend_port}",
            ],
            web_env,
            artifacts / "production-web.log",
        )
        worker = start(
            stack,
            [sys.executable, "manage.py", "judge_worker", "--worker-id=production-acceptance"],
            worker_env,
            artifacts / "production-worker.log",
        )
        proxy = start(
            stack,
            [str(caddy), "run", "--config", str(config), "--adapter", "caddyfile"],
            proxy_env,
            artifacts / "production-proxy.log",
        )
        client = Client(base, context)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in [web, worker, proxy]):
                raise RuntimeError(
                    "Deployment process exited; inspect test-results/production-*.log"
                )
            try:
                status, _, body = client.request("/health/")
                if status == 200 and json.loads(body)["evaluation"] == "available":
                    break
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Production deployment did not become ready")
        print(
            "Checking verified TLS, forwarding, body limits, cookies and the real browser journey",
            flush=True,
        )
        transport = transport_checks(base, http_port, backend_port, context)
        output = run(sys.executable, "tests/browser_smoke.py", env=web_env)
        (artifacts / "production-browser.log").write_bytes(output)
        output = run(
            sys.executable,
            "tests/browser_community.py",
            env=dict(web_env, **moderator_credentials),
        )
        (artifacts / "production-community-browser.log").write_bytes(output)
        output = run(sys.executable, "tests/browser_i18n.py", env=web_env)
        (artifacts / "production-i18n-browser.log").write_bytes(output)
        output = run(sys.executable, "tests/browser_projects_i18n.py", env=web_env)
        (artifacts / "production-projects-i18n-browser.log").write_bytes(output)
        output = run(
            sys.executable,
            "tests/browser_practice.py",
            env=dict(web_env, BROWSER_CA_FILE=str(temporary / "ca.pem")),
            timeout=300,
        )
        (artifacts / "production-practice-browser.log").write_bytes(output)
        from scripts.advanced_browsers import run_advanced_browsers

        owner_environment = dict(env)

        def owner_run(*arguments, env=None):
            fixture_env = dict(owner_environment)
            fixture_env.update(
                {key: value for key, value in (env or {}).items() if key.startswith("BROWSER_")}
            )
            return run(sys.executable, *arguments, env=fixture_env)

        advanced = run_advanced_browsers(
            dict(web_env, BROWSER_CA_FILE=str(temporary / "ca.pem")),
            owner_run,
            artifacts / "production-advanced",
            include_challenges=True,
            include_object_api=True,
        )
        print("Measuring eight-client browsing and a six-account real submission burst", flush=True)
        baseline = browse(base, context)
        jobs, during_judge, signed_in, source = load_jobs(base, context, password, owner_url)
        print(
            "Stopping the owned worker and checking fail-closed admission after heartbeat expiry",
            flush=True,
        )
        stop(worker)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            status, _, body = client.request("/health/")
            if status == 200 and json.loads(body)["evaluation"] == "unavailable":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("Dead worker was still advertised as available")
        assert signed_in.submit(source)[0] == 503
        with psycopg.connect(owner_url) as connection:
            assert (
                connection.execute("SELECT count(*) FROM core_submission").fetchone()[0]
                == JOB_SAMPLES
            )
        assert client.request("/practice/")[0] == 200
        assert (
            signed_in.request(
                "/problems/sum-positive/draft/",
                {"source": source, "version": signed_in.version},
                {"Accept": "application/json"},
            )[0]
            == 200
        )
        # All attempts share one real client address despite distinct forged headers.
        limited = [
            Client(base, context, source_address="127.0.0.3").request(
                "/accounts/login/", {"username": f"unknown-{i}"}, {"X-Real-IP": f"192.0.2.{i + 1}"}
            )[0]
            for i in range(65)
        ]
        # A fixed-window boundary can permit up to two windows of 30 requests.
        assert 429 in limited and 403 in limited and set(limited) <= {403, 429}, limited
        result = {
            "passed": True,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "scope": "loopback private CA; not a public deployment or hosted CI result",
            "caddy": caddy_version.split()[0],
            "caddy_binary_sha256": hashlib.sha256(caddy.read_bytes()).hexdigest(),
            "host": {
                "python": platform.python_version(),
                "machine": platform.machine(),
                "available_cpus": len(os.sched_getaffinity(0)),
                "web_cpu_quota": None,
                "postgres_memory_mib": 512,
            },
            "postgres_image": POSTGRES,
            "app_processes": 2,
            "threads_per_process": 2,
            "judge_workers": 1,
            "wasi_manifest_sha256": env["JUDGE_WASI_SHA256"],
            "tls": transport,
            "database_roles": {
                "worker_auth_read_denied": True,
                "worker_learning_and_community_read_denied": True,
                "worker_api_showcase_translations_projects_read_denied": True,
                "web_translations_projects_write_denied": True,
                "worker_organizations_and_coaching_read_denied": True,
                "web_package_ownership_and_publication_lock_write_denied": True,
                "web_challenge_definitions_read_only": True,
                "worker_challenge_definitions_and_participation_read_denied": True,
                "web_ddl_denied": True,
                "web_content_and_health_write_denied": True,
            },
            "browser": {
                "real_judge": True,
                "community_moderation_workflow": True,
                "english_workflow": True,
                "local_cli_api_projects_and_public_results": True,
                "hsts": True,
                "secure_cookies": True,
                "certificate_exception": "ephemeral leaf SPKI only",
                "advanced": advanced,
            },
            "browse_idle": baseline,
            "browse_during_judge": during_judge,
            "judge_burst": jobs,
            "worker_outage": {
                "submission_status": 503,
                "no_new_job": True,
                "reading_and_drafts_available": True,
            },
            "spoofed_client_ip_rate_limit": True,
            "percentile_method": "nearest rank",
            "local_regression_targets": {
                "browse_p95_seconds": 1,
                "six_submission_burst_seconds": 60,
            },
        }
    result["cleanup_passed"] = True
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    print("PASS: temporary database, services, private keys and credentials cleaned up", flush=True)


if __name__ == "__main__":
    main()
