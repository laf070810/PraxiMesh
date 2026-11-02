"""Durable trusted container crash/upgrade recovery with fixed CPU guests only."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_connection import ContainerConnection  # noqa: E402
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_journal import ContainerJournal, PersistentRuntimeService, RecoveryContainer  # noqa: E402
from judge.container_runtime_service import SOURCES, RuntimeContainerService, contract_digest  # noqa: E402
from judge.container_service import PREFIX, LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402
from scripts.container_runtime_acceptance import build_image, read_report  # noqa: E402
from scripts.container_supervision_acceptance import gone, owned_cleanup, tree  # noqa: E402

EXTRA = (
    "judge/container_compiler.py",
    "scripts/container_journal_acceptance.py",
    "scripts/container_runtime_acceptance.py",
    "scripts/container_supervision_acceptance.py",
)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def child(path, *, recover=False):
    config = json.loads((path / "config.json").read_text())
    engine = LocalEngine(config["socket"])
    calls = []
    original_run = engine.run

    def only_recovery(*args, **kwargs):
        calls.append(args[:2])
        if args[:2] in {("container", "start"), ("container", "create"), ("container", "exec")}:
            raise AssertionError("Recovery must not execute code")
        return original_run(*args, **kwargs)

    if recover:
        engine.run = only_recovery
    with DeviceLock(path / "locks", config["device"]) as guard:
        with ContainerJournal(path / "journal", guard, engine) as journal:
            if recover:
                before = journal.load()
                assert before is not None
                changed = before["labels"][PREFIX + "runtime"] != contract_digest()
                if config["phase"] == "upgrade":
                    assert changed
                    old = PersistentRuntimeService(
                        journal, config["job"], config["reservation"], image=config["image"]
                    )
                    try:
                        old.inspect()
                    except SandboxUnavailable:
                        pass
                    else:
                        raise AssertionError("Current execution accepted an old source contract")
                result = journal.recover()
                assert (
                    result["removed"] is True
                    and result["ready"] is False
                    and journal.load() is None
                )
                save(
                    path / "recovered.json",
                    {
                        "result": result,
                        "different_source_contract": changed,
                        "contract": contract_digest(),
                        "calls": calls,
                        "pid": os.getpid(),
                    },
                )
                return
            service = PersistentRuntimeService(
                journal, config["job"], config["reservation"], image=config["image"]
            )
            phase = config["phase"]
            if phase in {"intent", "unrecorded-create", "created"}:
                name = "record_intent" if phase == "intent" else "record_created"
                original = getattr(service, name)

                def interrupt(*args):
                    if phase != "unrecorded-create":
                        original(*args)
                    os._exit(17)

                setattr(service, name, interrupt)
            status = service.start(
                [PYTHON, "-I", "/opt/praximesh/container_runtime_fixture.py", "deadline"],
                deadline=time.monotonic() + 65,
                on_tick=lambda: None,
            )
            read_report(engine, service, "runtime-ready", time.monotonic() + 12)
            engine.run(
                "container",
                "exec",
                service.container_id,
                PYTHON,
                "-I",
                "-c",
                f"from pathlib import Path; Path('{STATE}/observe').touch(exist_ok=False)",
            )
            read_report(engine, service, "runtime-busy", time.monotonic() + 5)
            processes = tree(engine, service)
            save(
                path / "owner.json",
                {
                    "container": service.container_id,
                    "processes": processes,
                    "status": status,
                    "pid": os.getpid(),
                },
            )
            if phase == "archived":
                original_write = journal.write

                def archived(name, value, **kwargs):
                    original_write(name, value, **kwargs)
                    if name.endswith(".done.json"):
                        os._exit(17)

                journal.write = archived
                journal.recover()
                raise AssertionError("Expected archive interruption")
            os._exit(17)


def fresh(engine, journal, config):
    service = PersistentRuntimeService(
        journal, config["job"], config["reservation"], image=config["image"]
    )
    status = service.start(
        [PYTHON, "-I", "/opt/praximesh/container_transport_fixture.py", "normal"],
        deadline=time.monotonic() + 40,
        on_tick=lambda: None,
    )
    canary = os.urandom(32).hex()
    with ContainerConnection(service, status, on_tick=lambda: None) as connection:
        connection.channel.send({"event": "probe", "canary": canary, "padding": "x" * 1024**2})
        value = connection.channel.receive()
        assert value["event"] == "guest-ready"
        assert value["identity"]["sha256"] == hashlib.sha256(canary.encode()).hexdigest()
        processes = tree(engine, service)
        connection.channel.send({"event": "finish"})
        assert connection.channel.receive() == {"event": "completed", "recovered": True}
        connection.finish()
    controller = read_report(engine, service, "transport", time.monotonic() + 5)
    assert controller["completed"] is True and len(controller["cleanup"]) == 1
    result = journal.recover()
    gone(processes)
    assert journal.load() is None
    return {
        "container": service.container_id,
        "result": result,
        "guest_cleanup": controller["cleanup"],
        "process_tree_gone": True,
        "nonce": status["nonce"],
    }


def stage_upgrade(directory):
    for name in (*SOURCES, *EXTRA):
        target = directory / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    with (directory / "judge/container_journal.py").open("a") as stream:
        stream.write(
            "\n# Fixed acceptance revision: changed code identity, unchanged v1 recovery.\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket")
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--owner", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recover", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.owner or options.recover:
        return child(options.owner or options.recover, recover=options.recover is not None)
    if not options.docker_socket or not options.runtime_archive:
        parser.error("--docker-socket and --runtime-archive are required")
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    out = ROOT / "test-results" / ("px-container-journal-" + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "checks": [],
        "cleanup": [],
        "contract": contract_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (*SOURCES, *EXTRA)
        },
    }
    result_path = out / "result.json"
    save(result_path, report)
    print(json.dumps({"report": str(result_path)}), flush=True)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, out, out.name)
        report["image"] = image
        upgraded = out / "upgraded"
        stage_upgrade(upgraded)
        for phase in (
            "intent",
            "unrecorded-create",
            "created",
            "running",
            "upgrade",
            "replacement",
            "archived",
        ):
            path = out / phase
            path.mkdir(mode=0o700)
            config = {
                "socket": options.docker_socket,
                "image": image,
                "phase": phase,
                "device": "GPU-" + str(uuid.uuid4()),
                "job": str(uuid.uuid4()),
                "reservation": str(uuid.uuid4()),
            }
            save(path / "config.json", config)
            record = {"phase": phase}
            recovery = replacement = None
            try:
                result = subprocess.run(
                    [sys.executable, "-I", __file__, "--owner", str(path)],
                    capture_output=True,
                    timeout=85,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                )
                assert result.returncode == 17, result.stderr.decode()[-3000:]
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        before = journal.load()
                        assert before is not None
                        record["interrupted_record"] = before
                        recovery = RecoveryContainer(journal, before)
                        state = recovery.inspect()
                        if phase == "intent":
                            assert state is None and before["container_id"] is None
                        elif phase in {"unrecorded-create", "created"}:
                            assert state is not None and state["status"] == "created"
                            assert (before["container_id"] is None) == (
                                phase == "unrecorded-create"
                            )
                        elif phase == "archived":
                            assert state is None and before["phase"] == "removed"
                        else:
                            assert state["running"] is True
                        if phase == "replacement":
                            recovery.stop()
                            recovery.remove()
                            replacement = RuntimeContainerService(
                                guard,
                                config["job"],
                                config["reservation"],
                                engine=engine,
                                image=image,
                            )
                            replacement.owner, replacement.name = before["owner"], before["name"]
                            replacement.start(
                                ["/bin/sleep", "30"],
                                deadline=time.monotonic() + 40,
                                on_tick=lambda: None,
                            )
                            try:
                                journal.recover()
                            except SandboxUnavailable:
                                pass
                            else:
                                raise AssertionError(
                                    "A different container was treated as the original"
                                )
                            assert (
                                replacement.inspect()["running"] is True
                                and journal.load() == before
                            )
                            record["replacement_refused_and_untouched"] = replacement.container_id
                            replacement.stop()
                            replacement.remove()
                            report["cleanup"].append(
                                {"container": replacement.container_id, "removed": True}
                            )
                script = (
                    upgraded / "scripts/container_journal_acceptance.py"
                    if phase == "upgrade"
                    else Path(__file__)
                )
                with (path / "recovery.log").open("x") as log:
                    subprocess.run(
                        [sys.executable, "-I", str(script), "--recover", str(path)],
                        check=True,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=35,
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "LC_ALL": "C.UTF-8",
                        },
                    )
                record["recovery"] = json.loads((path / "recovered.json").read_text())
                owner_path = path / "owner.json"
                if owner_path.exists():
                    original = json.loads(owner_path.read_text())
                    gone(original["processes"])
                    assert original["pid"] != record["recovery"]["pid"]
                    record["process_tree_gone"] = True
                if recovery.container_id:
                    report["cleanup"].append({"container": recovery.container_id, "removed": True})
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        record["fresh"] = fresh(engine, journal, config)
                        assert record["fresh"]["nonce"] != before["arguments"][2]
                        report["cleanup"].append(
                            {"container": record["fresh"]["container"], "removed": True}
                        )
                record["passed"] = True
                report["checks"].append(record)
                save(result_path, report)
                print("PASS " + phase, flush=True)
            except BaseException as error:
                record["failure"] = type(error).__name__ + ": " + str(error)
                report["failed_scenario"] = record
                # Failed acceptance still removes only its own recorded identity.
                with DeviceLock(path / "locks", config["device"]) as guard:
                    with ContainerJournal(path / "journal", guard, engine) as journal:
                        pending = journal.load()
                        if pending is not None:
                            owned_cleanup(
                                engine, RecoveryContainer(journal, pending), image, report
                            )
                raise
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
    finally:
        try:
            if image is not None:
                engine.run("image", "rm", "--no-prune", out.name)
                report["owned_image_tag_removed"] = True
        finally:
            save(result_path, report)
    report["passed"] = True
    save(result_path, report)
    print(json.dumps({"passed": True, "report": str(result_path)}), flush=True)


if __name__ == "__main__":
    main()
