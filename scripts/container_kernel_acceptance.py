"""Actual CPU/runsc kernel protocol and recovery flow; no CUDA or GPU mappings."""

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
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON, WORKLOAD_PROFILE  # noqa: E402
from judge.container_isolation_protocol import resource_request  # noqa: E402
from judge.container_journal import ContainerJournal, PersistentRuntimeService  # noqa: E402
from judge.container_kernel_protocol import checked_completion  # noqa: E402
from judge.container_runtime_service import SOURCES, contract_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_contract import project_public_problem  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_kernel_checks import check_request  # noqa: E402
from judge.gpu_task_protocol import public_task_request, task_request  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_kernel_fixture import IDENTITY  # noqa: E402
from scripts.container_runtime_acceptance import build_image, read_report  # noqa: E402
from scripts.container_supervision_acceptance import gone, tree  # noqa: E402


def problem():
    return {
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "operation": "vector-add-f32-v1",
        "entrypoint": "solve",
        "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        "tests": [
            {
                "name": "public",
                "public": True,
                "inputs": {"a": [1.0, -2.0], "b": [0.5, 7.0]},
                "expected": [1.5, 5.0],
            },
            {
                "name": "protected",
                "public": False,
                "inputs": {"a": [13.0], "b": [-2.0]},
                "expected": [11.0],
            },
        ],
    }


def request_for(test_mode):
    if test_mode.startswith("public"):
        request = public_task_request(
            project_public_problem(problem()), "Opaque CPU fixture data, never compiled"
        )
        if test_mode == "public-protected":
            request["problem"]["tests"].append(problem()["tests"][-1])
        elif test_mode == "public-submit":
            request["mode"] = "submit"
        elif test_mode != "public":
            raise ValueError("Unknown public CPU fixture scenario")
        return request
    if test_mode == "recover":
        return {"event": "recover"}
    if test_mode == "check-resources":
        return resource_request(uuid.uuid4().hex)
    if test_mode.startswith("check-"):
        name = test_mode.removeprefix("check-")
        if name == "unexpected":
            name = "vector-subtraction"
        return check_request(name, uuid.uuid4().hex)
    request = task_request(
        problem(),
        "Opaque CPU fixture data, never compiled",
        "run" if test_mode == "invalid" else test_mode,
    )
    if test_mode == "invalid":
        request["untrusted_extra"] = True
    return request


def scenario(engine, image, path, mode, test_mode):
    request = request_for(test_mode)
    path.mkdir(mode=0o700)
    row = {"mode": mode, "test_mode": test_mode, "passed": False, "fixture_only": True}
    connection = None
    with DeviceLock(path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
        with ContainerJournal(path / "journal", guard, engine) as journal:
            service = PersistentRuntimeService(
                journal,
                uuid.uuid4(),
                uuid.uuid4(),
                image=image,
                resource_profile=WORKLOAD_PROFILE,
                purpose="maintenance"
                if test_mode == "recover" or test_mode.startswith("check-")
                else "task",
            )
            try:
                status = service.start(
                    [PYTHON, "-I", "/opt/praximesh/container_kernel_fixture.py", mode],
                    deadline=time.monotonic() + (180 if test_mode == "check-resources" else 100),
                    on_tick=lambda: None,
                )
                row["container"] = service.container_id
                cancel_at = None

                def tick():
                    if cancel_at is not None and time.monotonic() >= cancel_at:
                        raise SandboxUnavailable("Fixed CPU acceptance cancellation")

                connection = ContainerConnection(service, status, on_tick=tick)
                channel = connection.channel
                channel.send({"event": "prepare-cpu-fixture"})
                assert channel.receive() == IDENTITY
                row["processes"] = tree(engine, service)
                channel.send(request)
                if mode == "hang":
                    cancel_at = time.monotonic() + 1
                if mode in {"recovery-fail", "hang"} or test_mode in {
                    "invalid",
                    "check-unexpected",
                    "public-protected",
                    "public-submit",
                }:
                    try:
                        channel.receive()
                    except SandboxUnavailable as error:
                        if mode == "hang":
                            assert "acceptance cancellation" in str(error)
                    else:
                        raise AssertionError("Failed CPU task returned a result")
                    connection.close()
                    cancel_at = None
                    row["result_withheld"] = True
                else:
                    value = checked_completion(channel.receive(), request)
                    if test_mode.startswith("check-"):
                        row["startup_check"] = value["report"]
                    elif test_mode != "recover":
                        expected = {
                            "normal": "accepted",
                            "wrong-answer": "wrong_answer",
                            "syntax-error": "syntax_error",
                            "runtime-error": "runtime_error",
                        }[mode]
                        assert value["result"]["verdict"] == expected
                        assert ("performance" in value["result"]) == (
                            test_mode == "submit" and expected == "accepted"
                        )
                        if test_mode == "public":
                            assert value["formal_result"] is False
                            assert value["result"]["total"] == 1
                        row["verdict"] = expected
                    channel.send({"event": "finish"})
                    connection.finish()
                    row["guest_cleanup"] = value["cleanup"]
                controller = read_report(engine, service, "kernel-fixture", time.monotonic() + 15)
                row["controller"] = controller
                assert controller["gpu_executed"] is False and controller["fixture_only"] is True
                assert controller["passed"] == (not row.get("result_withheld", False))
                if test_mode in {"invalid", "public-protected", "public-submit"}:
                    assert not controller["cleanup"]
                if mode == "recovery-fail":
                    assert "correctness recovery failed" in controller["failure"]
                row["passed"] = True
            except BaseException as error:
                row["failure"] = type(error).__name__ + ": " + str(error)
                try:
                    row["controller"] = read_report(
                        engine, service, "kernel-fixture", time.monotonic() + 2
                    )
                except Exception:
                    pass
                raise
            finally:
                try:
                    if connection is not None:
                        connection.close()
                finally:
                    row["cleanup"] = journal.recover()
                    if row.get("processes"):
                        gone(row["processes"])
                    row["process_tree_gone"] = True
                    save(path / "result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument(
        "--resources-only",
        action="store_true",
        help="Only resource checks, failed recovery, and a new normal task; no GPU",
    )
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-kernel-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    source_names = (
        *SOURCES,
        "scripts/container_runtime_acceptance.py",
        "scripts/container_kernel_acceptance.py",
    )
    report = {
        "passed": False,
        "gpu_executed": False,
        "fixture_only": True,
        "resources_only": options.resources_only,
        "checks": [],
        "runtime_contract": contract_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_names
        },
    }
    save(out / "result.json", report)
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    base = Path(tempfile.mkdtemp(prefix=out.name + "-"))
    report["storage"] = str(base)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, out, out.name)
        report["image"] = image
        scenarios = (
            ("normal", "run"),
            ("normal", "submit"),
            ("wrong-answer", "submit"),
            ("syntax-error", "run"),
            ("runtime-error", "run"),
            ("recovery-fail", "run"),
            ("hang", "run"),
            ("normal", "invalid"),
            ("normal", "public"),
            ("wrong-answer", "public"),
            ("syntax-error", "public"),
            ("runtime-error", "public"),
            ("recovery-fail", "public"),
            ("hang", "public"),
            ("normal", "public-protected"),
            ("normal", "public-submit"),
            ("normal", "public"),
            ("normal", "recover"),
            ("normal", "check-public-run"),
            ("wrong-answer", "check-vector-subtraction"),
            ("check-hang", "check-cancel"),
            ("check-hang", "check-attach-death"),
            ("normal", "check-unexpected"),
            ("recovery-fail", "check-public-run"),
            ("normal", "check-resources"),
            ("recovery-fail", "check-resources"),
            ("normal", "run"),
        )
        if options.resources_only:
            scenarios = (
                ("normal", "check-resources"),
                ("recovery-fail", "check-resources"),
                ("normal", "run"),
            )
        for mode, test_mode in scenarios:
            row = scenario(engine, image, base / str(len(report["checks"])), mode, test_mode)
            report["checks"].append(row)
            save(out / "result.json", report)
            print("PASS " + mode + " " + test_mode, flush=True)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        # Each scenario recovers only its own journal before this storage copy.
        shutil.copytree(base, out / "scenarios")
        pending = list(base.glob("*/journal/*.active.json"))
        if pending:
            report["retained_storage"] = str(base)
            report["passed"] = False
        else:
            shutil.rmtree(base)
            report["storage_removed"] = True
        if image is not None:
            engine.run("image", "rm", "--no-prune", out.name)
            report["image_tag_removed"] = True
        save(out / "result.json", report)
    assert report["passed"]
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
