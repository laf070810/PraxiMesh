"""Owned CPU runsc-in-container acceptance. No CUDA or product admission."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_runtime_service import SOURCES, RuntimeContainerService, contract_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_artifacts import ARCHIVE_SHA512, runtime_from_archive  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from scripts.container_supervision_acceptance import gone, owned_cleanup, tree, wait_exit  # noqa: E402


def stage_context(archive, out):
    context = out / "context"
    context.mkdir(mode=0o700)
    snapshot = context / "runtime.tar.bz2"
    # Verify the private copy; do not stage arbitrary paths or inherited config.
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as incoming, snapshot.open("xb") as outgoing:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024**2:
            raise ValueError("A bounded regular runtime archive is required")
        size = 0
        while chunk := incoming.read(1024 * 1024):
            size += len(chunk)
            if size > info.st_size:
                raise ValueError("Runtime archive changed during staging")
            outgoing.write(chunk)
        if size != info.st_size:
            raise ValueError("Runtime archive changed during staging")
    runtime_from_archive(snapshot, context / "runtime")
    snapshot.unlink()
    shutil.copyfile(ROOT / "deploy/container-runtime.Dockerfile", context / "Dockerfile")
    shutil.copyfile(ROOT / "vendor/gvisor/LICENSE", context / "gvisor-LICENSE")
    for name in ("container_entry.py", "container_contract.py"):
        shutil.copyfile(ROOT / "judge" / name, context / name)
    for name in (
        "container_runtime_fixture.py",
        "container_transport_fixture.py",
        "container_workload_fixture.py",
        "gpu_runtime_cpu_guest.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, context / name)
    (context / "judge").mkdir()
    host_only = {
        "judge/container_service.py",
        "judge/container_runtime_service.py",
        "judge/container_connection.py",
        "judge/container_journal.py",
        "judge/container_entry.py",
    }
    for path in (
        ROOT / name for name in SOURCES if name.startswith("judge/") and name not in host_only
    ):
        shutil.copyfile(path, context / "judge" / path.name)
    return context


def build_context(engine, context, out, tag, digest_function, *, expected_digest):
    config = out / "docker-config"
    config.mkdir(mode=0o700)
    digest = digest_function()
    if digest != expected_digest:
        raise RuntimeError("Runtime source changed during staging")
    with (out / "build.log").open("w") as log:
        subprocess.run(
            [
                "docker",
                "--config",
                str(config),
                "--host",
                "unix://" + str(engine.path),
                "build",
                "--tag",
                tag,
                "--iidfile",
                str(out / "image-id"),
                "--build-arg",
                "RUNTIME_CONTRACT=" + digest,
                str(context),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
        )
    if digest != digest_function():
        raise RuntimeError("Runtime source changed during build")
    return (out / "image-id").read_text().strip()


def build_image(engine, archive, out, tag):
    expected = contract_digest()
    return build_context(
        engine, stage_context(archive, out), out, tag, contract_digest, expected_digest=expected
    )


def read_report(engine, service, name, until):
    while time.monotonic() < until:
        result = engine.run(
            "container",
            "exec",
            service.container_id,
            PYTHON,
            "-I",
            "-c",
            f"from pathlib import Path; import sys; sys.stdout.buffer.write(Path('{STATE}/{name}.json').read_bytes()[:65537])",
            check=False,
        )
        if result.returncode == 0:
            if len(result.stdout) > 65536:
                raise RuntimeError("Runtime report exceeded its budget")
            return json.loads(result.stdout)
        if not service.inspect()["running"]:
            raise RuntimeError("Supervisor stopped before its CPU report was collected")
        time.sleep(0.1)
    raise RuntimeError("Fixed CPU runtime report timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-container-runtime-" + token)
    out.mkdir(mode=0o700)
    tag = out.name
    report = {
        "passed": False,
        "gpu_executed": False,
        "scope": "Fixed CPU guest checks; aggregate outer limits, no GPU readiness",
        "checks": [],
        "cleanup": [],
        "runtime_archive_sha512": ARCHIVE_SHA512,
        "contract": contract_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (*SOURCES, "scripts/container_runtime_acceptance.py")
        },
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, out, tag)
        report["image"] = image
        for mode in ("checks", "orphan", "deadline"):
            path = out / mode
            path.mkdir(mode=0o700)
            with DeviceLock(path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
                service = RuntimeContainerService(
                    guard, uuid.uuid4(), uuid.uuid4(), engine=engine, image=image
                )
                try:
                    status = service.start(
                        [PYTHON, "-I", "/opt/praximesh/container_runtime_fixture.py", mode],
                        deadline=time.monotonic() + (150 if mode == "checks" else 25),
                        on_tick=lambda: None,
                    )
                    ready = read_report(engine, service, "runtime-ready", time.monotonic() + 12)
                    assert ready["state"]["status"] == "running"
                    processes = tree(engine, service)
                    report.setdefault("processes", {})[mode] = processes
                    engine.run(
                        "container",
                        "exec",
                        service.container_id,
                        PYTHON,
                        "-I",
                        "-c",
                        f"from pathlib import Path; Path('{STATE}/observe').touch(exist_ok=False)",
                    )
                    if mode == "checks":
                        result = read_report(engine, service, "runtime", status["deadline"] - 5)
                        report["runtime"] = result
                        assert result["passed"], result
                        service.stop()
                    else:
                        read_report(engine, service, "runtime-busy", time.monotonic() + 5)
                        wait_exit(service, 0 if mode == "orphan" else 124, status["deadline"] + 5)
                    gone(processes)
                    service.remove()
                    report["cleanup"].append({"container": service.container_id, "removed": True})
                    report["checks"].append(
                        {"name": mode, "passed": True, "process_tree_gone": True}
                    )
                    save()
                    print("PASS " + mode, flush=True)
                except BaseException as error:
                    report["failure"] = type(error).__name__ + ": " + str(error)
                    if service.container_id:
                        details = engine.run(
                            "container",
                            "exec",
                            service.container_id,
                            PYTHON,
                            "-I",
                            "-c",
                            f"from pathlib import Path; import sys; sys.stdout.buffer.write(Path('{STATE}/runtime.json').read_bytes()[:65537])",
                            check=False,
                        )
                        if details.returncode == 0 and len(details.stdout) <= 65536:
                            report["runtime"] = json.loads(details.stdout)
                    raise
                finally:
                    owned_cleanup(engine, service, image, report)
                    save()
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
    finally:
        try:
            if image is not None:
                engine.run("image", "rm", tag)
                report["owned_image_tag_removed"] = True
        finally:
            save()
    report["passed"] = True
    save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
