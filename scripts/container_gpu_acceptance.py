"""Real single-card node propagation and cleanup, using CPU-only fixed guests."""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_controller import CONTROLLER_SOURCES  # noqa: E402
from judge.container_gpu import SOURCES, gpu_task_digest  # noqa: E402
from judge.container_monitor import devices, validate_observer  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_monitor import identify, processes, query  # noqa: E402
from judge.nvml_artifacts import read_manifest  # noqa: E402
from scripts.build_container_driver import build_image  # noqa: E402
from scripts.container_controller_acceptance import (  # noqa: E402
    Control,
    build_controller,
    same_identity,
    state,
    task_tree,
    wait_report,
)
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_supervision_acceptance import gone  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument(
        "--device", required=True, help="Explicit authorized physical GPU UUID; metadata only"
    )
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--nvrtc-archive", required=True, type=Path)
    parser.add_argument("--docker-cli-archive", required=True, type=Path)
    parser.add_argument("--driver-library-dir", required=True, type=Path)
    options = parser.parse_args()
    target = identify(options.device)
    before = query("-i", options.device, "--query", "--xml-format")
    if processes(before, target):
        raise ValueError("Selected GPU has external processes; nothing was stopped")
    minor = ET.fromstring(before).findtext("gpu/minor_number", "")
    if not minor.isascii() or not minor.isdecimal():
        raise ValueError("Expected one selected character-device minor")
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-gpu-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    source_names = tuple(
        dict.fromkeys((*SOURCES, *CONTROLLER_SOURCES, "scripts/container_gpu_acceptance.py"))
    )
    report = {
        "passed": False,
        "gpu_executed": False,
        "cuda_initialized": False,
        "task_contract": gpu_task_digest(),
        "checks": [],
        "cleanup": [],
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_names
        },
    }
    save(out / "result.json", report)
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    base = Path(tempfile.mkdtemp(prefix="px-container-gpu-"))
    report["storage"] = str(base)
    controls, tags = [], []
    try:
        task_dir, control_dir = out / "task", out / "controller"
        task_dir.mkdir()
        control_dir.mkdir()
        task_tag, control_tag = out.name + "-task", out.name + "-controller"
        tags = [(task_tag, task_dir), (control_tag, control_dir)]
        task_image, driver, _compiler = build_image(
            engine,
            options.runtime_archive,
            options.nvrtc_archive,
            options.driver_library_dir,
            task_dir,
            task_tag,
            device_tasks=True,
        )
        control_image = build_controller(
            engine,
            options.docker_cli_archive,
            control_dir,
            control_tag,
            nvml_library_dir=options.driver_library_dir,
            full_inventory=True,
        )
        observer = validate_observer(
            {
                "schema": 2,
                "target": target,
                "minor": int(minor),
                "library": read_manifest(control_dir / "context/observer"),
            }
        )
        report.update(
            task_image=task_image,
            controller_image=control_image,
            observer=observer,
            driver_manifest=driver,
        )
        path = config = None
        original_processes = {}
        for name in ("normal", "wrong-uvm", "orphan", "recover"):
            if name != "recover":
                path = base / name
                config = state(
                    engine, path, task_image, host_processes=True, device_observer=observer
                )
                save(path / "driver.json", driver)
            else:
                (path / "active.json").unlink()
            mode = "gpu-nodes" + ("" if name == "normal" else "-" + name)
            control = Control(
                engine,
                control_image,
                path,
                config,
                mode,
                host_processes=True,
                observation_devices=tuple(devices(observer)),
            )
            controls.append(control)
            row = {"name": name, "passed": False}
            report["checks"].append(row)
            control.start()
            row["controller"] = control.container_id
            task_processes = {}
            if name != "wrong-uvm":
                active = wait_report(control, path, "active")
                same_identity(active, config)
                task_processes = task_tree(engine, active)
                row.update(active=active, original_processes=task_processes)
                assert (
                    active["record"]["version"] == 3
                    and active["record"]["device_spec"] == active["selection"]
                )
                assert active["guest"]["identity"]["cuda_initialized"] is False
                if name == "orphan":
                    control.wait(17)
                    original_processes = task_processes
                else:
                    (path / "continue").touch(mode=0o600)
            if name != "orphan":
                finished = wait_report(control, path, "finished")
                control.wait()
                assert finished["ready"] is False and finished["completion"]["removed"] is True
                gone(task_processes)
                row.update(finished=finished, process_tree_gone=True)
                report["cleanup"].append(finished["completion"])
                if name == "recover":
                    gone(original_processes)
                    recovered = json.loads((path / "recovered.json").read_text())
                    assert recovered["ready"] is False and recovered["removed"] is True
                    row["original_process_tree_gone"] = True
                    report["cleanup"].append(recovered)
            row["passed"] = True
            report["cleanup"].append(control.cleanup())
            save(out / "result.json", report)
            print("PASS " + name, flush=True)
        assert not processes(query("-i", options.device, "--query", "--xml-format"), target)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in report["sources"].items()
        )
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            for control in controls:
                report["cleanup"].append(control.cleanup())
            # Recover only records created in this private test root, under their original lock.
            from judge.container_journal import ContainerJournal
            from judge.gpu_device_lock import DeviceLock

            for directory in base.iterdir():
                config_path = directory / "config.json"
                if not config_path.exists():
                    continue
                configured = json.loads(config_path.read_text())
                with DeviceLock(directory / "locks", configured["device"]) as guard:
                    with ContainerJournal(directory / "journal", guard, engine) as journal:
                        result = journal.recover()
                        if result is not None:
                            report["cleanup"].append(result)
                shutil.copytree(directory, out / ("state-" + directory.name))
            report["tag_cleanup"] = []
            for tag, directory in tags:
                image_id = directory / "image-id"
                if not image_id.exists():
                    continue
                identifier = image_id.read_text().strip()
                info = json.loads(engine.run("image", "inspect", tag).stdout)
                assert len(info) == 1 and info[0]["Id"] == identifier
                engine.run("image", "rm", "--no-prune", tag)
                report["tag_cleanup"].append({"tag": tag, "removed": True})
            shutil.rmtree(base)
            report["storage_removed"] = True
        except BaseException as error:
            report["cleanup_failure"] = type(error).__name__ + ": " + str(error)
            report["retained_storage"] = str(base)
            raise
        finally:
            save(out / "result.json", report)
    report["passed"] = True
    save(out / "result.json", report)
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
