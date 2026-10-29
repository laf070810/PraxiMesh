"""Replay one-command preview, persistence and cleanup with a unique Compose project."""

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Trusted synthetic fixtures, never learner code. The session key is kept only
# in controller memory; command output containing it is not saved to logs.
FIXTURE = r"""
import hashlib, json, os, secrets, stat, sys
from pathlib import Path
key = Path('/data/preview-secret.key')
os.environ['APP_SECRET_KEY'] = key.read_text()
os.environ['DJANGO_SETTINGS_MODULE'] = 'praximesh.settings'
import django
django.setup()
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from core.models import AccountProfile, Draft, KnowledgeNode, Problem
from projects.models import Project, ProjectStage
from challenges.models import Challenge
from localization.models import KnowledgeTranslation, ProblemTranslation
counts = dict(nodes=KnowledgeNode.objects.filter(published=True).count(),
    problems=Problem.objects.filter(published=True).count(),
    paths=KnowledgeNode.objects.filter(kind='path', published=True).count(),
    projects=Project.objects.filter(published=True).count(), stages=ProjectStage.objects.count(),
    challenges=Challenge.objects.filter(published=True).count(),
    translated_nodes=KnowledgeTranslation.objects.filter(published=True).count(),
    translated_problems=ProblemTranslation.objects.filter(published=True).count())
if sys.argv[1] == 'prepare':
    assert counts == dict(nodes=50, problems=26, paths=6, projects=1, stages=4,
        challenges=1, translated_nodes=20, translated_problems=10), counts
    user = get_user_model().objects.create_user('compose-preview-learner', password=secrets.token_urlsafe(32))
    AccountProfile.objects.create(user=user, recovery_hash=make_password(secrets.token_urlsafe(32)))
    Draft.objects.create(user=user, version=Problem.objects.get(pk='sum-positive').current,
        source='# Synthetic private draft; never executed')
    original = KnowledgeNode.objects.filter(kind='concept').first()
    data = dict(original.data, id='compose-retained-node', title='Retained local concept')
    KnowledgeNode.objects.create(id=data['id'], kind=original.kind, title=data['title'], data=data)
    Problem.objects.filter(pk='dot-product').update(published=False)
    session = SessionStore()
    session['_auth_user_id'] = str(user.pk)
    session['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
    session['_auth_user_hash'] = user.get_session_auth_hash()
    session.save()
    session_key = session.session_key
else:
    user = get_user_model().objects.get(username='compose-preview-learner')
    assert Draft.objects.get(user=user).source == '# Synthetic private draft saved over HTTP; never executed'
    assert KnowledgeNode.objects.get(pk='compose-retained-node').published
    assert not Problem.objects.get(pk='dot-product').published
    session_key = None
assert os.getuid() == 65532
assert stat.S_IMODE(key.stat().st_mode) == 0o600
print(json.dumps(dict(counts=counts, session_key=session_key, user_id=user.pk,
    version_id=Problem.objects.get(pk='sum-positive').current_id,
    key_sha256=hashlib.sha256(key.read_bytes()).hexdigest())))
"""


def main():
    project = "px-preview-" + uuid.uuid4().hex[:12]
    artifacts = ROOT / "test-results" / project
    artifacts.mkdir(parents=True)
    environment = dict(os.environ, PRAXIMESH_PORT="0")
    for name in (
        "APP_SECRET_KEY",
        "APP_ALLOWED_HOSTS",
        "PRAXIMESH_BIND",
        "PYTHON_IMAGE",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
    ):
        environment.pop(name, None)
    report = {"project": project, "passed": False}
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    number = 0
    # UDP connect only chooses the local source address; no datagram is sent.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as routing:
        routing.connect(("192.0.2.1", 9))
        host = routing.getsockname()[0]
    assert not ipaddress.ip_address(host).is_loopback and host != "0.0.0.0", host

    def run(*arguments, private=False, timeout=600):
        nonlocal number
        number += 1
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if not private:
            (artifacts / f"{number:02d}.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Docker operation {number} failed; inspect {artifacts}")
        return result.stdout.strip()

    def compose(*arguments, **kwargs):
        return run(
            "compose",
            "--project-name",
            project,
            "--file",
            str(ROOT / "compose.yaml"),
            "--env-file",
            os.devnull,
            *arguments,
            **kwargs,
        )

    def address():
        published = compose("port", "web", "8000")
        assert published.startswith("0.0.0.0:"), published
        return f"http://{host}:" + published.rsplit(":", 1)[1]

    def get(base, path, session=None):
        headers = {"Cookie": "sessionid=" + session} if session else {}
        with client.open(
            urllib.request.Request(base + path, headers=headers), timeout=10
        ) as response:
            assert response.status == 200 and response.url == base + path, path
            return response.read()

    def save_draft(base, fixture):
        page = base + "/problems/sum-positive/"
        session = "sessionid=" + fixture["session_key"]
        with client.open(
            urllib.request.Request(page, headers={"Cookie": session}), timeout=10
        ) as response:
            cookies = SimpleCookie()
            for value in response.headers.get_all("Set-Cookie", []):
                cookies.load(value)
            csrf = cookies["csrftoken"].value
        payload = urllib.parse.urlencode(
            {
                "source": "# Synthetic private draft saved over HTTP; never executed",
                "version": fixture["version_id"],
                "csrfmiddlewaretoken": csrf,
            }
        ).encode()
        headers = {
            "Cookie": session + "; csrftoken=" + csrf,
            "Accept": "application/json",
            "Referer": page,
            "Origin": "http://other-host.invalid",
        }
        request = urllib.request.Request(page + "draft/", data=payload, headers=headers)
        try:
            client.open(request, timeout=10).close()
        except urllib.error.HTTPError as error:
            assert error.code == 403
            error.close()
        else:
            raise AssertionError("Cross-origin POST was not rejected")
        headers["Origin"] = base
        request = urllib.request.Request(page + "draft/", data=payload, headers=headers)
        with client.open(request, timeout=10) as response:
            assert response.status == 200 and json.load(response) == {"saved": True}

    def owned(resource):
        return run(
            resource,
            "ls",
            "--quiet",
            *(["--all"] if resource == "container" else []),
            "--filter",
            "label=com.docker.compose.project=" + project,
        )

    for resource in ("container", "network", "volume"):
        assert not owned(resource)
    try:
        config = json.loads(compose("config", "--format", "json"))
        assert config["services"]["web"]["environment"]["APP_SECRET_KEY"] == ""
        assert config["services"]["web"]["environment"]["APP_ALLOWED_HOSTS"] == "*"
        assert config["services"]["web"]["ports"][0]["host_ip"] == "0.0.0.0"
        print("Building and starting the default Compose preview", flush=True)
        compose("up", "--build", "--detach", "--wait", "--wait-timeout", "180")
        container = compose("ps", "--quiet", "web")
        info = json.loads(run("inspect", container, private=True))[0]
        assert info["Config"]["User"] == "65532:65532"
        assert info["HostConfig"]["ReadonlyRootfs"] and info["HostConfig"]["Init"]
        assert info["HostConfig"]["Memory"] == 512 * 1024 * 1024
        assert info["HostConfig"]["PidsLimit"] == 64
        assert "ALL" in info["HostConfig"]["CapDrop"]
        assert "/data" in {item["Destination"] for item in info["Mounts"]}
        assert all(
            item["Destination"] in {"/data", "/tmp"} and item["Type"] != "bind"
            for item in info["Mounts"]
        )
        base = address()
        assert json.loads(get(base, "/health/")) == {"status": "ok", "evaluation": "unavailable"}
        paths = [
            "/",
            "/learn/",
            "/practice/",
            "/projects/",
            "/challenges/",
            "/community/",
            "/accounts/register/",
            "/static/app.css",
            "/static/app.js",
        ]
        for path in paths:
            assert get(base, path), path
        first = json.loads(
            compose("exec", "-T", "web", "python", "-c", FIXTURE, "prepare", private=True)
        )
        get(base, "/me/", first["session_key"])
        save_draft(base, first)
        print("Initial catalog, HTTP, session and container boundaries passed", flush=True)
        compose("logs", "--no-color", "web")
        compose("down")
        print("Recreating the service with the preserved data volume", flush=True)
        compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "180")
        second = json.loads(
            compose("exec", "-T", "web", "python", "-c", FIXTURE, "verify", private=True)
        )
        assert first["key_sha256"] == second["key_sha256"]
        assert first["user_id"] == second["user_id"]
        get(address(), "/me/", first["session_key"])
        report.update(
            passed=True,
            initial_catalog=first["counts"],
            http_paths=paths,
            nonroot_readonly_without_docker_socket=True,
            account_draft_session_custom_content_and_withdrawal_preserved=True,
            secret_preserved=True,
            published_bind="0.0.0.0",
            verified_access_host=host,
            same_origin_draft_post_passed=True,
            cross_origin_post_rejected=True,
            source_sha256={
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("compose.yaml", "core/preview.py", "Dockerfile", "requirements.lock")
            },
        )
    finally:
        try:
            compose("logs", "--no-color", "web")
        finally:
            compose("down", "--volumes", "--rmi", "local")
            for resource in ("container", "network", "volume"):
                assert not owned(resource)
            report["owned_resources_removed"] = True
            (artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: Compose initialization, persistent data/session/key, HTTP and cleanup", flush=True)
    print("Evidence: " + str(artifacts / "result.json"), flush=True)


if __name__ == "__main__":
    main()
