"""Real device-free compilation from a mapped trusted control container."""

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_compile_protocol import validate_compiled  # noqa: E402
from judge.container_compiler import CompilerContainerService, compiler_digest  # noqa: E402
from judge.container_contract import WORKLOAD_PROFILE, resource_limits  # noqa: E402
from judge.container_controller import CONTROLLER_SOURCES, controller_digest  # noqa: E402
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.sandbox import policy_digest  # noqa: E402
from scripts.build_container_compiler import build_image  # noqa: E402
from scripts.container_compiler_acceptance import cases  # noqa: E402
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

EXTRA = (
    "scripts/controller_compiler_acceptance.py",
    "scripts/container_controller_acceptance.py",
    "scripts/container_compiler_acceptance.py",
    "content/candidates/cuda.json",
    "tests/cuda_incorrect_solutions.json",
)


def scenarios():
    messages = {name: message for name, message, _expected in cases()}
    reference = messages["cuda-vector-add-reference"]
    return [
        ("vector", reference, "compile-normal", "compiled"),
        ("matrix", messages["cuda-matrix-product-reference"], "compile-normal", "compiled"),
        ("syntax-error", messages["syntax-error"], "compile-normal", "syntax_error"),
        (
            "unsupported-architecture",
            messages["unsupported-architecture"],
            "compile-normal",
            "platform_error",
        ),
        ("invalid-request", messages["invalid-request"], "compile-invalid", "invalid_request"),
        ("wrong-image", reference, "compile-bad-image", "image_refused"),
        ("cancel-before-source", reference, "compile-cancel-before", "cancelled"),
        ("recover-begin", reference, "compile-normal", "compiled"),
        ("cancel-after-compile", reference, "compile-cancel", "cancelled"),
        ("recover-cancel", reference, "compile-normal", "compiled"),
        ("controller-exit", reference, "compile-orphan", "orphan"),
        ("recover-exit", reference, "compile-normal", "compiled"),
        ("controller-kill", reference, "compile-normal", "killed"),
        ("recover-kill", reference, "compile-normal", "compiled"),
    ]


def fresh_config(path, config, image, engine):
    # Same installation, device and journal; each invocation has new ownership.
    with DeviceLock(path / "locks", config["device"]) as guard:
        with ContainerJournal(path / "journal", guard, engine) as journal:
            assert journal.load() is None
            assert journal.identity == config["installation"]
            config = {
                **config,
                "image": image,
                "job": str(uuid.uuid4()),
                "reservation": str(uuid.uuid4()),
            }
            service = CompilerContainerService(
                journal,
                config["job"],
                config["reservation"],
                image=image,
                resource_profile=WORKLOAD_PROFILE,
            )
            config.update(owner=service.owner, name=service.name)
    for filename in ("active.json", "prepared.json", "finished.json", "send-source", "continue"):
        (path / filename).unlink(missing_ok=True)
    save(path / "config.json", config)
    return config


def verify_active(value, config):
    same_identity(value, config)
    assert value["gpu_executed"] is False and value["begin_calls"] == 1
    assert value["status"]["limits"] == resource_limits(WORKLOAD_PROFILE)
    assert value["original_record"] == {
        "version": 2,
        "resource_profile": WORKLOAD_PROFILE,
        "owner": config["owner"],
        "image": config["image"],
        "container_id": value["container"],
    }


def observer_rejections(engine, control_image, path, config, report, controls, result_path):
    original = json.loads((path / "mounts.json").read_text())
    assert original["version"] == 2
    try:
        for name in ("missing-proc", "wrong-proc", "wrong-boot", "writable-proc"):
            value = copy.deepcopy(original)
            if name == "wrong-proc":
                value["host_processes"]["root"][1] += 1
            elif name == "wrong-boot":
                value["host_processes"]["boot"] = str(uuid.uuid4())
            save(path / "mounts.json", value)
            control = Control(
                engine,
                control_image,
                path,
                config,
                "hold",
                host_processes=name != "missing-proc",
                proc_readonly=name != "writable-proc",
            )
            controls.append(control)
            control.start()
            logs = control.wait(1)
            assert logs.strip() in {"SandboxUnavailable", "FileNotFoundError"}, logs
            assert not (path / "held.json").exists()
            with DeviceLock(path / "locks", config["device"]) as guard:
                with ContainerJournal(path / "journal", guard, engine) as journal:
                    assert journal.load() is None
                    assert journal.identity == config["installation"]
            report["cleanup"].append(control.cleanup())
            report["checks"].append(
                {
                    "name": name,
                    "passed": True,
                    "controller": control.container_id,
                    "task_created": False,
                    "failure_class": logs.strip(),
                }
            )
            save(result_path, report)
            print("PASS " + name, flush=True)
    finally:
        save(path / "mounts.json", original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--nvrtc-archive", required=True, type=Path)
    parser.add_argument("--docker-cli-archive", required=True, type=Path)
    parser.add_argument(
        "--process-observer",
        action="store_true",
        help="Use the explicit read-only host PID view and verify ownership fences",
    )
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-controller-compiler-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "process_observer": options.process_observer,
        "compiler_contract": compiler_digest(),
        "controller_contract": controller_digest(),
        "gpu_execution_digest": execution_digest(),
        "cpu_policy_digest": policy_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (*CONTROLLER_SOURCES, *EXTRA)
        },
        "checks": [],
        "cleanup": [],
    }
    result_path = out / "result.json"
    save(result_path, report)
    print(json.dumps({"report": str(result_path)}), flush=True)
    controls, tags = [], []
    storage = tempfile.TemporaryDirectory(prefix=out.name + "-")
    path, config = Path(storage.name) / "state", None
    report["storage"] = storage.name
    try:
        for name in ("compiler", "controller"):
            (out / name).mkdir(mode=0o700)
        tags.append(out.name + "-compiler")
        image, artifacts = build_image(
            engine, options.runtime_archive, options.nvrtc_archive, out / "compiler", tags[-1]
        )
        report.update(compiler_image=image, compiler_artifacts=artifacts)
        tags.append(out.name + "-controller")
        control_image = build_controller(
            engine, options.docker_cli_archive, out / "controller", tags[-1]
        )
        report["controller_image"] = control_image
        config = state(engine, path, image, host_processes=options.process_observer)
        if options.process_observer:
            observer_rejections(engine, control_image, path, config, report, controls, result_path)
        for name, message, mode, expected in scenarios():
            if (
                options.process_observer
                and mode == "compile-normal"
                and expected in {"compiled", "killed"}
            ):
                mode = "compile-observe"
            config = fresh_config(
                path, config, control_image if mode == "compile-bad-image" else image, engine
            )
            save(path / "input.json", message)
            row = {"name": name, "expected": expected, "passed": False, "processes": {}}
            report["checks"].append(row)
            control = Control(
                engine, control_image, path, config, mode, host_processes=options.process_observer
            )
            controls.append(control)
            row["controller"] = control.container_id
            control.start()
            early = expected in {"invalid_request", "image_refused"}
            if not early:
                prepared = wait_report(control, path, "prepared")
                verify_active(prepared, config)
                row["prepared"] = prepared
                row["processes"].update(task_tree(engine, prepared))
                (path / "send-source").touch()
            compiled = expected in {"compiled", "orphan", "killed"} or mode == "compile-cancel"
            if compiled:
                active = wait_report(control, path, "active")
                verify_active(active, config)
                validate_compiled(active["compilation"], message["architecture"])
                row["active"] = active
                if mode == "compile-observe":
                    observed = active["process_observation"]
                    assert len(observed) >= 3 and active["private_pid"] == 1
                    assert len(active["rejected_host_pids"]) == 2
                    assert all(
                        item["cgroup"] == active["observer_anchor"]["cgroup"] for item in observed
                    )
                    assert active["observer_anchor"]["pid"] != 1
                row["processes"].update(task_tree(engine, active))
                if expected == "killed":
                    control.kill()
                else:
                    (path / "continue").touch()
            if expected in {"orphan", "killed"}:
                if expected == "orphan":
                    control.wait(17)
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        original = journal.load()
                        assert original["owner"] == config["owner"]
                        assert original["container_id"] == row["active"]["container"]
                        row["recovery"] = journal.recover()
                        assert journal.load() is None
                        report["cleanup"].append(row["recovery"])
            else:
                finished = wait_report(control, path, "finished")
                same_identity(finished, config)
                assert finished["journal_empty"] is True and finished["gpu_executed"] is False
                assert finished["begin_calls"] == (0 if early else 1)
                if expected == "cancelled":
                    assert finished["cancelled"] is True and "result" not in finished
                    assert ("compilation" in finished) == (mode == "compile-cancel")
                elif early:
                    assert finished[expected] is True and finished["container"] is None
                    assert finished["cleanup"] is None
                else:
                    result = finished["result"]
                    assert result["status"] == expected
                    assert result["ready"] is False and result["executed"] is False
                    assert result["artifacts"] == {
                        "image": image,
                        "compiler_contract": compiler_digest(),
                        "resource_profile": WORKLOAD_PROFILE,
                        "limits": resource_limits(WORKLOAD_PROFILE),
                    }
                row["finished"] = finished
                if mode == "compile-observe":
                    assert finished["cleaned_observer_rejected"] is True
                if finished["cleanup"] is not None:
                    report["cleanup"].append(finished["cleanup"])
                assert control.wait() == ""
            gone(row["processes"])
            row["process_tree_gone"] = True
            report["cleanup"].append(control.cleanup())
            row["passed"] = True
            save(result_path, report)
            print("PASS " + name, flush=True)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        errors = []
        for control in controls:
            try:
                if control.inspect() is not None:
                    report["cleanup"].append(control.cleanup())
            except BaseException as error:
                errors.append(type(error).__name__ + ": " + str(error))
        if config is not None:
            try:
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        if journal.load() is not None:
                            report["cleanup"].append(journal.recover())
            except BaseException as error:
                errors.append(type(error).__name__ + ": " + str(error))
        report["tag_cleanup"] = []
        for tag in tags:
            removed = engine.run("image", "rm", "--no-prune", tag, check=False).returncode == 0
            report["tag_cleanup"].append({"tag": tag, "removed": removed})
            if not removed:
                errors.append("Test image tag removal failed: " + tag)
        if errors:
            report.update(passed=False, cleanup_errors=errors, retained_storage=storage.name)
            storage._finalizer.detach()
        else:
            storage.cleanup()
            report["storage_removed"] = True
        save(result_path, report)
        if errors:
            raise RuntimeError("Owned cleanup incomplete; inspect the private report")
    print(json.dumps({"passed": True, "report": str(result_path)}), flush=True)


if __name__ == "__main__":
    main()
