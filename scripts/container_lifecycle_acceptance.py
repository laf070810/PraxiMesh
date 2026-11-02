"""Real nested CPU controller faults and new recovery; no GPU or readiness."""

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
from judge.container_journal import ContainerJournal, PersistentRuntimeService  # noqa: E402
from judge.container_kernel_protocol import checked_completion  # noqa: E402
from judge.container_lifecycle import observe_fault, validate_report  # noqa: E402
from judge.container_runtime_service import SOURCES, contract_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.controller_faults import CHECKS, fault_request  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_task_protocol import task_request  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_kernel_acceptance import problem  # noqa: E402
from scripts.container_kernel_fixture import IDENTITY  # noqa: E402
from scripts.container_runtime_acceptance import build_image, read_report  # noqa: E402
from scripts.container_supervision_acceptance import gone, tree  # noqa: E402


def invoke(engine, image, journal, job, reservation, row, *, request, mode="normal"):
    """Same production flow/observer in an explicitly device-free task service."""
    fault = request["event"] == "check-controller"
    service = PersistentRuntimeService(
        journal,
        job,
        reservation,
        image=image,
        purpose="task" if request["event"] == "execute" else "maintenance",
        resource_profile=WORKLOAD_PROFILE,
    )
    connection = None
    processes = {}
    row.update(passed=False, gpu_executed=False, fixture_only=True, mode=mode)
    try:
        started = time.monotonic()
        deadline = started + (45 if fault else 80)
        status = service.start(
            [PYTHON, "-I", "/opt/praximesh/container_kernel_fixture.py", mode],
            deadline=deadline,
            on_tick=journal.guard.assert_held,
        )
        row.update(container_id=service.container_id, nonce=status["nonce"], supervision=status)
        connection = ContainerConnection(service, status, on_tick=journal.guard.assert_held)
        channel = connection.channel
        channel.send({"event": "prepare-cpu-fixture"})
        assert channel.receive() == IDENTITY
        processes = tree(engine, service)
        row["processes"] = processes
        channel.send(request)
        if mode in {"fault-prepare-fail", "recovery-fail"}:
            try:
                channel.receive()
            except SandboxUnavailable:
                row["result_withheld"] = True
            else:
                raise AssertionError("Failed fixed correctness check returned a ready result")
            connection.close()
            fixture = read_report(engine, service, "kernel-fixture", time.monotonic() + 10)
            assert (
                fixture["passed"] is False and "correctness recovery failed" in fixture["failure"]
            )
            row["fixture"] = fixture
        elif fault:
            armed = channel.receive()
            # Sample the complete live guest tree after arming, before injecting.
            processes = tree(engine, service)
            row["processes"] = processes
            row["observation"] = observe_fault(
                connection,
                service,
                request,
                armed,
                started=started,
                deadline=deadline,
                on_tick=journal.guard.assert_held,
            )
            if row["observation"]["independent_stop"]:
                # Observe disappearance before journal.recover() can issue any
                # stop/remove command; cleanup must not manufacture timer proof.
                gone(processes)
                row["independent_process_tree_gone"] = True
        else:
            completed = checked_completion(channel.receive(), request)
            if request["event"] == "execute":
                assert completed["result"]["verdict"] == "accepted"
            row["completed"] = completed
            channel.send({"event": "finish"})
            connection.finish()
        row["passed"] = True
    except BaseException as error:
        row["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            row["cleanup"] = journal.recover()
            gone(processes)
            row["process_tree_gone"] = True
            assert journal.load() is None
    if "observation" in row:
        row["observation"]["cleanup"] = row["cleanup"]
        validate_report(row["observation"], request, device_uuid=journal.guard.device_uuid)


def scenario(engine, image, directory, name, *, prepare_failure=False, recovery_failure=False):
    directory.mkdir(mode=0o700)
    row = {
        "name": name,
        "passed": False,
        "prepare_failure": prepare_failure,
        "recovery_failure": recovery_failure,
    }
    request = fault_request(name, uuid.uuid4().hex)
    try:
        with DeviceLock(directory / "locks", "GPU-" + str(uuid.uuid4())) as guard:
            with ContainerJournal(directory / "journal", guard, engine) as journal:
                job, reservation = uuid.uuid4(), uuid.uuid4()
                row["fault"], row["recovery"] = {}, {}
                invoke(
                    engine,
                    image,
                    journal,
                    job,
                    reservation,
                    row["fault"],
                    request=request,
                    mode="fault-prepare-fail" if prepare_failure else "normal",
                )
                invoke(
                    engine,
                    image,
                    journal,
                    job,
                    reservation,
                    row["recovery"],
                    request={"event": "recover"},
                    mode="recovery-fail" if recovery_failure else "normal",
                )
                fault, recovery = row["fault"], row["recovery"]
                assert fault["container_id"] != recovery["container_id"]
                assert fault["nonce"] != recovery["nonce"]
                if not prepare_failure and not recovery_failure:
                    report = {
                        **fault["observation"],
                        "recovered": True,
                        "recovery": {
                            key: recovery[key] for key in ("container_id", "nonce", "cleanup")
                        },
                    }
                    validate_report(report, request, device_uuid=guard.device_uuid, recovered=True)
                    row["report"] = report
                else:
                    row["success_report_withheld"] = True
                # Return to a normal Run after maintenance, including after a
                # deliberately failed recovery. No prior invocation is reused.
                row["next_task"] = {}
                invoke(
                    engine,
                    image,
                    journal,
                    job,
                    reservation,
                    row["next_task"],
                    request=task_request(problem(), "Opaque fixed CPU fixture data", "run"),
                )
                assert row["next_task"]["container_id"] not in {
                    fault["container_id"],
                    recovery["container_id"],
                }
                row["passed"] = True
    except BaseException as error:
        row["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        save(directory / "result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-lifecycle-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "fixture_only": True,
        "checks": [],
        "runtime_contract": contract_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                *SOURCES,
                "scripts/container_runtime_acceptance.py",
                "scripts/container_kernel_acceptance.py",
                "scripts/container_lifecycle_acceptance.py",
            )
        },
    }
    base = Path(tempfile.mkdtemp(prefix=out.name + "-"))
    report["storage"] = str(base)
    save(out / "result.json", report)
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, out, out.name)
        report["image"] = image
        for name, prepare_failure, recovery_failure in (
            *((name, False, False) for name in CHECKS),
            ("controller-exit", True, False),
            ("controller-exit", False, True),
        ):
            row = scenario(
                engine,
                image,
                base / str(len(report["checks"])),
                name,
                prepare_failure=prepare_failure,
                recovery_failure=recovery_failure,
            )
            report["checks"].append(row)
            save(out / "result.json", report)
            print("PASS " + name + " " + str((prepare_failure, recovery_failure)), flush=True)
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        shutil.copytree(base, out / "scenarios")
        if list(base.glob("*/journal/*.active.json")):
            report.update(passed=False, retained_storage=str(base))
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
