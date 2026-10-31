"""Real NVRTC compilation inside runsc without any GPU device or CUDA driver."""

import argparse
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
from judge.container_compile_protocol import (  # noqa: E402
    request,
    validate_compiled,
    validate_identity,
)
from judge.container_compiler import SOURCES, CompilerContainerService, compiler_digest  # noqa: E402
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import (  # noqa: E402
    PYTHON,
    RESOURCE_PROFILES,
    SUPERVISION_PROFILE,
    WORKLOAD_PROFILE,
    resource_limits,
)
from judge.container_journal import ContainerJournal  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.sandbox import policy_digest  # noqa: E402
from scripts.build_container_compiler import build_image  # noqa: E402
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_supervision_acceptance import gone, tree, wait_exit  # noqa: E402
from scripts.container_workload_checks import run_resource  # noqa: E402

EXTRA = (
    "scripts/container_compiler_acceptance.py",
    "scripts/container_workload_checks.py",
    "content/candidates/cuda.json",
    "tests/cuda_incorrect_solutions.json",
)


def cases():
    package = json.loads((ROOT / "content/candidates/cuda.json").read_text())
    incorrect = json.loads((ROOT / "tests/cuda_incorrect_solutions.json").read_text())
    rows = []
    for problem in package["problems"]:
        rows.append(
            (
                problem["id"] + "-reference",
                request(problem["reference"], problem["operation"], [8, 6]),
                "compiled",
            )
        )
        for index, item in enumerate(incorrect[problem["id"]]):
            rows.append(
                (
                    problem["id"] + "-wrong-" + str(index),
                    request(item["source"], problem["operation"], [8, 6]),
                    "compiled",
                )
            )
    reference = rows[0][1]
    rows.extend(
        [
            (
                "syntax-error",
                {**reference, "source": 'extern "C" __global__ void solve( {'},
                "syntax_error",
            ),
            ("host-code", {**reference, "source": "int main() { return 0; }"}, "syntax_error"),
            ("unsupported-architecture", {**reference, "architecture": [99, 9]}, "platform_error"),
            ("invalid-request", {**reference, "verdict": "accepted"}, "invalid_request"),
            ("invalid-architecture", {**reference, "architecture": [True, 6]}, "invalid_request"),
            ("recover-errors", reference, "compiled"),
            ("disconnect", reference, "disconnect"),
            ("recover-disconnect", reference, "compiled"),
            ("deadline", reference, "deadline"),
            ("recover-deadline", reference, "compiled"),
        ]
    )
    return rows


def run_case(
    engine,
    image,
    journal,
    name,
    message,
    expected,
    report,
    output,
    *,
    resource_profile=SUPERVISION_PROFILE,
):
    service = CompilerContainerService(
        journal,
        uuid.uuid4(),
        uuid.uuid4(),
        image=image,
        resource_profile=resource_profile,
    )
    connection = None
    processes = {}
    record = {
        "name": name,
        "expected": expected,
        "passed": False,
        "resource_profile": resource_profile,
        "source_sha256": hashlib.sha256(message["source"].encode()).hexdigest(),
    }
    report["checks"].append(record)
    save(output / "result.json", report)
    try:
        deadline = time.monotonic() + (30 if expected == "deadline" else 80)
        status = service.start(
            [
                PYTHON,
                "-I",
                "/opt/praximesh/container_compile_entry.py",
                "deadline" if expected == "deadline" else "normal",
            ],
            deadline=deadline,
            on_tick=lambda: None,
        )
        record["container"] = service.container_id
        assert status["limits"] == resource_limits(resource_profile)
        record["supervision"] = status
        connection = ContainerConnection(service, status, on_tick=lambda: None)
        channel = connection.channel
        channel.send(message)
        initial = channel.receive()
        if expected == "invalid_request":
            assert initial == {
                "event": "compile-rejected",
                "status": "invalid_request",
                "guest_started": False,
                "ready": False,
            }, initial
            record["result"] = initial
        else:
            validate_identity(initial)
            record["identity"] = initial
            processes = tree(engine, service)
            record["processes"] = processes
            channel.send({"event": "begin"})
            value = channel.receive()
            if expected in {"compiled", "disconnect", "deadline"}:
                assert set(value) == {"event", "compilation"} and value["event"] == "compiled", (
                    value
                )
                validate_compiled(value["compilation"], message["architecture"])
                record["compilation"] = value["compilation"]
                if expected == "disconnect":
                    connection.close()
                    connection = None
                    wait_exit(service, 70, time.monotonic() + 8)
                    gone(processes)
                    record["stopped_before_recovery"] = True
                    record["process_tree_gone_before_recovery"] = True
                elif expected == "deadline":
                    wait_exit(service, 124, status["deadline"] + 5)
                    observed = time.monotonic()
                    assert observed >= status["deadline"]
                    gone(processes)
                    record["independent_deadline"] = status["deadline"]
                    record["observed_exit_at"] = observed
                    record["stopped_before_recovery"] = True
                    record["process_tree_gone_before_recovery"] = True
                else:
                    channel.send({"event": "finish"})
                    value = channel.receive()
            if expected not in {"disconnect", "deadline"}:
                assert value["event"] == "compile-checked" and value["status"] == expected, value
                assert value["ready"] is False and value["executed"] is False, value
                assert value["cleanup"] == [{"id": "compiler", "stopped": True, "removed": True}]
                record["result"] = value
        if expected not in {"disconnect", "deadline"}:
            channel.send({"event": "acknowledged"})
            connection.finish()
        record["passed"] = True
    except BaseException as error:
        record["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            try:
                if connection is not None:
                    connection.close()
            finally:
                record["original_record"] = journal.load()
                cleanup = journal.recover()
                if cleanup is not None:
                    report["cleanup"].append(cleanup)
                gone(processes)
                record["process_tree_gone"] = True
        except BaseException as error:
            record.update(passed=False, cleanup_failure=type(error).__name__ + ": " + str(error))
            report["retain_storage"] = True
            raise
        finally:
            save(output / "result.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--nvrtc-archive", required=True, type=Path)
    parser.add_argument(
        "--resource-profile", choices=tuple(RESOURCE_PROFILES), default=SUPERVISION_PROFILE
    )
    parser.add_argument(
        "--resource-probes",
        action="store_true",
        help="Run fixed CPU resource checks and fresh compilation recovery",
    )
    options = parser.parse_args()
    if options.resource_probes and options.resource_profile != WORKLOAD_PROFILE:
        parser.error("--resource-probes requires --resource-profile=" + WORKLOAD_PROFILE)
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    output = ROOT / "test-results" / ("px-container-compiler-" + uuid.uuid4().hex[:12])
    output.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "resource_profile": options.resource_profile,
        "resource_probes": options.resource_probes,
        "checks": [],
        "cleanup": [],
        "compiler_contract": compiler_digest(),
        "gpu_execution_digest": execution_digest(),
        "cpu_policy_digest": policy_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (*SOURCES, *EXTRA)
        },
    }
    save(output / "result.json", report)
    print(json.dumps({"report": str(output / "result.json")}), flush=True)
    image = None
    storage = tempfile.TemporaryDirectory(prefix="px-compiler-")
    try:
        image, artifacts = build_image(
            engine, options.runtime_archive, options.nvrtc_archive, output, output.name
        )
        report.update(image=image, artifacts=artifacts)
        save(output / "result.json", report)
        base = Path(storage.name)
        with DeviceLock(base / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            with ContainerJournal(base / "journal", guard, engine) as journal:
                try:
                    for index, (name, message, expected) in enumerate(cases()):
                        run_case(
                            engine,
                            image,
                            journal,
                            name,
                            message,
                            expected,
                            report,
                            output,
                            resource_profile=options.resource_profile,
                        )
                        print("PASS " + name, flush=True)
                        if index == 0 and options.resource_probes:
                            for resource in ("memory", "cpu", "storage", "pids"):
                                run_resource(engine, image, journal, resource, report, output)
                                print("PASS resource-" + resource, flush=True)
                                recovery = "recover-resource-" + resource
                                run_case(
                                    engine,
                                    image,
                                    journal,
                                    recovery,
                                    message,
                                    expected,
                                    report,
                                    output,
                                    resource_profile=options.resource_profile,
                                )
                                print("PASS " + recovery, flush=True)
                finally:
                    shutil.copytree(base / "journal", output / "journal")
        assert len(report["checks"]) == len(cases()) + (8 if options.resource_probes else 0)
        assert all(r["passed"] for r in report["checks"])
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in report["sources"].items()
        )
        assert compiler_digest() == report["compiler_contract"]
        assert execution_digest() == report["gpu_execution_digest"]
        assert policy_digest() == report["cpu_policy_digest"]
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if report.get("retain_storage"):
            report["retained_storage"] = storage.name
            storage._finalizer.detach()
        else:
            storage.cleanup()
        report["storage_removed"] = not Path(storage.name).exists()
        if image is not None:
            report["image_tag_removed"] = (
                engine.run("image", "rm", output.name, check=False).returncode == 0
            )
            if not report["image_tag_removed"]:
                report["passed"] = False
        save(output / "result.json", report)
    assert report["passed"]
    print(json.dumps({"passed": True, "report": str(output / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
