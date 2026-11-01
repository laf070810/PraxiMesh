"""Real Docker/HTTP Web recovery with owned port conflicts and SIGKILL points.

Uses a fixed CPU HTTP command derived from an explicitly supplied local Web
image. Coordinator/proxy metadata is synthetic; no DB, learner code or GPU is
used. The separate full deployment acceptance remains required.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.deploy import Deployment, DeploymentError, atomic_json, private_file  # noqa: E402
from scripts.deploy_acceptance import retain_failure_diagnostic  # noqa: E402
from scripts.deploy_web import LABEL  # noqa: E402


def docker(*args, input=None, timeout=90):
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, check=True, timeout=timeout
    ).stdout


class ProbeDeployment(Deployment):
    def environment(self, generation, role):
        return {}  # The fixed HTTP server has no database or application secrets.


def load(directory):
    private_file(directory / "fixture.json")
    fixture = json.loads((directory / "fixture.json").read_text())
    host = ProbeDeployment.__new__(ProbeDeployment)
    host.config = fixture["config"]
    host.directory, host.path = directory, directory / "deployment.json"
    host.state = json.loads(host.path.read_text())
    host.proxy = SimpleNamespace(probe=lambda: {"port": None})
    return host, fixture["generation"]


def http(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("Owned CPU HTTP server did not answer")


def interrupted(directory, point):
    host, generation = load(directory)
    run, save = host.run, host.save

    def crash():
        atomic_json(directory / "checkpoint.json", {"point": point, "pid": os.getpid()})
        os.kill(os.getpid(), signal.SIGKILL)

    def cut_run(*args, **kwargs):
        result = run(*args, **kwargs)
        if (point, args[0]) in {
            ("removed", "rm"),
            ("created-unpinned", "create"),
            ("started-unconfirmed", "start"),
        }:
            crash()
        return result

    def cut_save(*args, **kwargs):
        save(*args, **kwargs)
        value = host.state["web_replacements"][generation["id"]]
        if point == "created-pinned" and value["phase"] == "created":
            crash()

    host.run, host.save = cut_run, cut_save
    host.replace_web(generation)
    raise AssertionError("Expected process interruption did not occur")


def run(options, output, result):
    token = secrets.token_hex(6)
    site = secrets.token_hex(16)
    network = "px-web-recovery-" + token
    image_tag = "praximesh-web-recovery:" + token
    fixture_name = "px-web-fixture-" + token
    owner = "org.praximesh.web-acceptance=" + token
    image = None
    base_present = False
    holder = None
    child = None
    result["source_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in [
            "scripts/deploy.py",
            "scripts/deploy_web.py",
            "scripts/deploy_web_acceptance.py",
        ]
    }
    with tempfile.TemporaryDirectory(prefix="px-web-recovery-") as temporary:
        directory = Path(temporary)
        try:
            base = json.loads(docker("image", "inspect", options.image))[0]
            base_present = True
            if base["Id"] != options.image or base["Config"].get("User") != "65532:65532":
                raise ValueError("Expected a pinned local non-root PraxiMesh Web image")
            if base["Config"].get("Volumes") or base["Config"].get("Entrypoint"):
                raise ValueError("The fixed Web image must not declare volumes or an entrypoint")
            fixture_id = (
                docker(
                    "create",
                    "--name",
                    fixture_name,
                    "--label",
                    owner,
                    "--label",
                    "org.praximesh.site=" + site,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    options.image,
                )
                .decode()
                .strip()
            )
            # Never start the source container. Change only local image metadata;
            # a builder interpreting an image ID as a registry name is unnecessary.
            docker(
                "commit",
                "--pause=false",
                "--change",
                "LABEL " + owner,
                "--change",
                'CMD ["python", "-m", "http.server", "8000", "--directory", "/tmp"]',
                fixture_id,
                image_tag,
            )
            image = json.loads(docker("image", "inspect", image_tag))[0]["Id"]
            docker("network", "create", "--label", owner, network)
            generation = {
                "id": token,
                "database": "synthetic-no-database",
                "web": f"px-{token}-web",
                "worker": f"px-{token}-worker",
                "manifest": {"images": {"web": image}},
            }
            config = {
                "instance": "webprobe",
                "networks": {"frontend": network},
                "proxy": {"origin": "http://127.0.0.1:8000", "bind": "127.0.0.1"},
                "database": {
                    key: secrets.token_urlsafe(32)
                    for key in ("owner_password", "web_password", "worker_password")
                },
                "application": {
                    key: secrets.token_urlsafe(48) for key in ("secret_key", "worker_secret")
                },
            }
            atomic_json(directory / "fixture.json", {"config": config, "generation": generation})
            atomic_json(
                directory / "deployment.json",
                {
                    "site_id": site,
                    "current": generation,
                    "operation": {"phase": "recovering_current"},
                },
            )
            host, generation = load(directory)
            # A fixed owned port makes the RootlessKit restart failure deterministic.
            with socket.socket() as allocation:
                allocation.bind(("127.0.0.1", 0))
                original_port = allocation.getsockname()[1]
                args = host.runtime(generation, "web", generation["web"])
                original_id = (
                    docker(
                        "create",
                        *args,
                        "--restart=unless-stopped",
                        "--publish",
                        f"127.0.0.1:{original_port}:8000",
                        image,
                    )
                    .decode()
                    .strip()
                )
            docker("start", original_id)
            http(original_port)
            host.stop(generation)
            holder = socket.socket()
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", original_port))
            holder.listen(1)
            try:
                host.service(generation, "web")
            except DeploymentError as error:
                if not error.diagnostic:
                    raise
                diagnostic = json.loads((directory / error.diagnostic).read_text())
                if "address already in use" not in diagnostic["stderr"]:
                    raise AssertionError(
                        "The expected owned port conflict was not observed"
                    ) from None
                retain_failure_diagnostic(directory, output, error.diagnostic)
                result["original_failure"] = {
                    "diagnostic": error.diagnostic,
                    "container_id": original_id,
                    "port": original_port,
                    "command": diagnostic["command"],
                    "rootlesskit": "RootlessKit" in diagnostic["stderr"],
                }
            else:
                raise AssertionError("Expected the owned port holder to prevent restarting Web")
            host.replace_web(generation)
            new_port = host.port(generation)
            http(new_port)
            assert new_port != original_port
            assert host.inspect(generation["web"])["Id"] != original_id
            result["checks"].append(
                {
                    "name": "port-conflict-replacement",
                    "passed": True,
                    "conflict_still_held": holder.getsockname()[1] == original_port,
                    "new_port": new_port,
                    "http_status": 200,
                }
            )
            holder.close()
            holder = None
            for point in ("removed", "created-unpinned", "created-pinned", "started-unconfirmed"):
                host.stop(generation)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child",
                        str(directory),
                        "--checkpoint",
                        point,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                stdout, stderr = child.communicate(timeout=90)
                if child.returncode != -signal.SIGKILL:
                    atomic_json(
                        output / (point + "-failure.json"),
                        {
                            "exit_code": child.returncode,
                            "stdout": stdout[-65536:].decode(errors="replace"),
                            "stderr": stderr[-65536:].decode(errors="replace"),
                        },
                    )
                assert child.returncode == -signal.SIGKILL
                checkpoint = json.loads((directory / "checkpoint.json").read_text())
                assert checkpoint == {"point": point, "pid": child.pid}
                host, generation = load(directory)
                durable_phase = host.state["web_replacements"][generation["id"]]["phase"]
                host.stop(generation)
                host.replace_web(generation)
                http(host.port(generation))
                result["checks"].append(
                    {
                        "name": point,
                        "passed": True,
                        "exit_code": child.returncode,
                        "durable_phase": durable_phase,
                        "http_status": 200,
                    }
                )
                child = None
            # A different ID with the same image/name/site/intent is still unknown.
            saved = host.state["web_replacements"][generation["id"]]
            docker("rm", "--force", saved["new_id"])
            args = host.runtime(generation, "web", generation["web"])
            substitute = (
                docker(
                    "create",
                    *args,
                    "--restart=unless-stopped",
                    "--publish",
                    "127.0.0.1::8000",
                    "--label",
                    LABEL + "=" + saved["token"],
                    image,
                )
                .decode()
                .strip()
            )
            docker("start", substitute)
            try:
                host.stop(generation)
            except DeploymentError as error:
                assert "ID differs" in str(error)
            else:
                raise AssertionError("A same-name replacement was incorrectly stopped")
            info = host.inspect(generation["web"])
            assert info["Id"] == substitute and info["State"]["Running"]
            result["checks"].append({"name": "same-name-substitution-refused", "passed": True})
            result["image"] = image
            result["passed"] = True
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=15)
            if holder is not None:
                holder.close()
            # Reconcile creation before a CLI error: list only this probe's site.
            identifiers = (
                docker(
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "label=org.praximesh.site=" + site,
                )
                .decode()
                .split()
            )
            for identifier in identifiers:
                info = json.loads(docker("container", "inspect", identifier))[0]
                assert info["Config"]["Labels"].get("org.praximesh.web-acceptance") == token
                assert info["Image"] == (
                    options.image if info["Name"] == "/" + fixture_name else image
                )
                docker("rm", "--force", identifier)
            networks = (
                docker("network", "ls", "--quiet", "--filter", "label=" + owner).decode().split()
            )
            for identifier in networks:
                docker("network", "rm", identifier)
            images = docker("image", "ls", "--quiet", "--filter", "label=" + owner).decode().split()
            if images:
                owned = json.loads(docker("image", "inspect", image_tag))[0]
                assert owned["Config"]["Labels"]["org.praximesh.web-acceptance"] == token
                docker("image", "rm", "--no-prune", image_tag)
            assert not docker(
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=org.praximesh.site=" + site,
            ).strip()
            assert not docker("network", "ls", "--quiet", "--filter", "label=" + owner).strip()
            assert not docker("image", "ls", "--quiet", "--filter", "label=" + owner).strip()
            result["owned_resources_removed"] = True
            if base_present:
                assert (
                    json.loads(docker("image", "inspect", options.image))[0]["Id"] == options.image
                )
                result["base_image_preserved"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Existing pinned CPU Web image sha256:...")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--checkpoint", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.child:
        interrupted(options.child, options.checkpoint)
        return
    if (
        not options.image
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", options.image)
        or not options.output
    ):
        parser.error("--image must be a pinned local CPU Web image; --output is required")
    output = options.output.absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {
        "passed": False,
        "scope": "real CPU Docker/HTTP and SIGKILL; synthetic coordinator/proxy; no DB/GPU/learner code",
        "checks": [],
        "owned_resources_removed": False,
    }
    try:
        run(options, output, result)
    except Exception as error:
        result["failure_type"] = type(error).__name__
        if isinstance(error, subprocess.CalledProcessError):
            atomic_json(
                output / "docker-failure.json",
                {
                    "exit_code": error.returncode,
                    "stdout": (error.stdout or b"")[-65536:].decode(errors="replace"),
                    "stderr": (error.stderr or b"")[-65536:].decode(errors="replace"),
                },
            )
        raise
    finally:
        atomic_json(output / "result.json", result)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "checks": len(result["checks"]),
                "owned_resources_removed": result["owned_resources_removed"],
            }
        )
    )


if __name__ == "__main__":
    main()
