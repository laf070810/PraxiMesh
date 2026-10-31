"""Build and check a local CUDA driver image without exposing any GPU device."""

import argparse
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_compiler import CompilerContainerService  # noqa: E402
from judge.container_contract import PYTHON, STATE, WORKLOAD_PROFILE, resource_limits  # noqa: E402
from judge.container_driver import SOURCES, DriverContainerService, driver_digest, image_contract  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.driver_artifacts import manifest_digest  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.sandbox import SandboxUnavailable, policy_digest  # noqa: E402
from scripts.build_container_driver import build_image, verify_installed  # noqa: E402
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_runtime_acceptance import read_report  # noqa: E402
from scripts.container_supervision_acceptance import gone, tree, wait_exit  # noqa: E402


def run_case(engine, image, manifest, journal, name, report, output):
    service = DriverContainerService(
        journal,
        uuid.uuid4(),
        uuid.uuid4(),
        image=image,
        driver_manifest=manifest,
        resource_profile=WORKLOAD_PROFILE,
    )
    row = {"name": name, "passed": False}
    report["checks"].append(row)
    processes = {}
    try:
        status = service.start(
            [PYTHON, "-I", "/opt/praximesh/container_driver_fixture.py"],
            deadline=time.monotonic() + 150,
            on_tick=lambda: None,
        )
        row.update(container=service.container_id, supervision=status)
        save(output / "result.json", report)
        assert status["limits"] == resource_limits(WORKLOAD_PROFILE)
        ready = read_report(engine, service, "driver-ready", time.monotonic() + 30)
        assert ready["state"]["status"] == "running"
        assert ready["identity"]["manifest_sha256"] == manifest_digest(manifest)
        processes = tree(engine, service)
        row.update(ready=ready, processes=processes)
        if name == "interrupted":
            engine.run("container", "kill", "--signal=KILL", service.container_id)
            wait_exit(service, 137, time.monotonic() + 8)
        else:
            engine.run(
                "container",
                "exec",
                service.container_id,
                PYTHON,
                "-I",
                "-c",
                f"from pathlib import Path; Path('{STATE}/observe').touch(exist_ok=False)",
            )
            value = read_report(engine, service, "driver", status["deadline"] - 4)
            row["result"] = value
            assert value["passed"] and len(value["checks"]) == 5, value
            assert value["gpu_executed"] is False and value["cuda_initialized"] is False
        row["passed"] = True
    except BaseException as error:
        row["failure"] = type(error).__name__ + ": " + str(error)
        if service.container_id:
            try:
                row["diagnostic"] = read_report(engine, service, "driver", time.monotonic() + 1)
            except Exception:
                pass
        raise
    finally:
        try:
            row["original_record"] = journal.load()
            recovered = journal.recover()
            if recovered is not None:
                report["cleanup"].append(recovered)
            gone(processes)
            row["process_tree_gone"] = True
        except BaseException as error:
            row.update(passed=False, cleanup_failure=type(error).__name__ + ": " + str(error))
            report["retain_storage"] = True
            raise
        finally:
            save(output / "result.json", report)


def rejected_images(image, manifest, journal, report):
    changed = copy.deepcopy(manifest)
    first = next(iter(changed["libraries"].values()))
    first["sha256"] = "0" * 64
    for name, service in (
        (
            "wrong-manifest",
            DriverContainerService(
                journal, uuid.uuid4(), uuid.uuid4(), image=image, driver_manifest=changed
            ),
        ),
        (
            "device-free-compiler-refuses-driver",
            CompilerContainerService(journal, uuid.uuid4(), uuid.uuid4(), image=image),
        ),
    ):
        try:
            service.start(
                [PYTHON, "-I", "/opt/praximesh/container_driver_fixture.py"],
                deadline=time.monotonic() + 30,
                on_tick=lambda: None,
            )
        except SandboxUnavailable as error:
            assert journal.load() is None and service.container_id is None
            report["checks"].append(
                {"name": name, "passed": True, "container_created": False, "reason": str(error)}
            )
        else:
            journal.recover()
            raise RuntimeError("Mismatched driver image was accepted")


def preserve_journal(journal, output, report):
    # Also cover failures in an expected rejection, before run_case owns cleanup.
    pending = True
    try:
        pending = journal.load() is not None
    finally:
        if pending:
            report["retain_storage"] = True
        shutil.copytree(journal.directory, output / "journal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--nvrtc-archive", required=True, type=Path)
    parser.add_argument("--driver-library-dir", required=True, type=Path)
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    output = ROOT / "test-results" / ("px-container-driver-" + uuid.uuid4().hex[:12])
    output.mkdir(mode=0o700)
    sources = (*SOURCES, "scripts/container_driver_acceptance.py")
    report = {
        "passed": False,
        "gpu_executed": False,
        "cuda_initialized": False,
        "checks": [],
        "cleanup": [],
        "resource_profile": WORKLOAD_PROFILE,
        "driver_contract": driver_digest(),
        "gpu_execution_digest": execution_digest(),
        "cpu_policy": policy_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources
        },
    }
    save(output / "result.json", report)
    print(json.dumps({"report": str(output / "result.json")}), flush=True)
    base = Path(tempfile.mkdtemp(prefix="px-driver-"))
    report["storage"] = str(base)
    try:
        image, manifest, compiler = build_image(
            engine,
            options.runtime_archive,
            options.nvrtc_archive,
            options.driver_library_dir,
            output,
            output.name,
        )
        report.update(
            image=image,
            driver_manifest=manifest,
            compiler=compiler,
            image_contract=image_contract(manifest),
        )
        save(output / "result.json", report)
        with DeviceLock(base / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            with ContainerJournal(base / "journal", guard, engine) as journal:
                try:
                    rejected_images(image, manifest, journal, report)
                    for name in ("checks", "interrupted", "recovered"):
                        verify_installed(options.driver_library_dir, manifest)
                        run_case(engine, image, manifest, journal, name, report, output)
                        print("PASS " + name, flush=True)
                finally:
                    preserve_journal(journal, output, report)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in report["sources"].items()
        )
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            path = output / "image-id"
            if path.is_file():
                identifier = path.read_text().strip()
                current = json.loads(engine.run("image", "inspect", output.name).stdout)
                if len(current) != 1 or current[0]["Id"] != identifier:
                    raise RuntimeError("Owned driver image tag changed")
                engine.run("image", "rm", output.name)
                report["owned_image_tag_removed"] = True
        finally:
            if report.get("retain_storage"):
                report["retained_storage"] = str(base)
            else:
                shutil.rmtree(base)
                report["storage_removed"] = True
            save(output / "result.json", report)
    report["passed"] = True
    save(output / "result.json", report)
    print(json.dumps({"passed": True, "report": str(output / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
