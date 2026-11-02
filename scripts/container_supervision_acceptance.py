"""Build and test owned trusted CPU supervisor containers; never access a GPU."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_service import PREFIX, ContainerService, LocalEngine  # noqa: E402
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402

FIXTURE = "/opt/praximesh/container_supervision_fixture.py"


def service_for(path, guard, engine, image):
    config = json.loads((path / "config.json").read_text())
    return ContainerService(guard, config["job"], config["reservation"], engine=engine, image=image)


def owner(path):
    config = json.loads((path / "config.json").read_text())
    engine = LocalEngine(config["socket"])
    with DeviceLock(path / "locks", config["device"]) as guard:
        service = service_for(path, guard, engine, config["image"])
        status = service.start(
            [PYTHON, "-I", FIXTURE, "tree"],
            deadline=time.monotonic() + config["seconds"],
            on_tick=lambda: None,
        )
        (path / "owner.json").write_text(
            json.dumps({"container": service.container_id, "status": status})
        )
        # Deliberately leave this owned container running; parent recovers it.


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])
    except FileNotFoundError:
        return None


def tree(engine, service):
    raw = engine.run("container", "inspect", service.container_id)
    record = json.loads(raw.stdout)[0]
    main = record["State"]["Pid"]
    nspid = next(
        line.split()[1:]
        for line in Path(f"/proc/{main}/status").read_text().splitlines()
        if line.startswith("NSpid:")
    )
    assert int(nspid[0]) == main and int(nspid[-1]) == 1
    rows = (
        engine.run("container", "top", service.container_id, "-eo", "pid,ppid")
        .stdout.decode()
        .splitlines()[1:]
    )
    processes = {int(row.split()[0]): birth(int(row.split()[0])) for row in rows}
    assert main in processes and len(processes) >= 3 and all(processes.values())
    return processes


def gone(processes):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if all(birth(pid) != started for pid, started in processes.items()):
            return
        time.sleep(0.05)
    raise RuntimeError("An owned container process remains")


def read_fixture(engine, service, name):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        result = engine.run(
            "container",
            "exec",
            service.container_id,
            PYTHON,
            "-I",
            "-c",
            f"from pathlib import Path; import sys; sys.stdout.buffer.write(Path('{STATE}/{name}.json').read_bytes()[:4096])",
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        time.sleep(0.1)
    raise RuntimeError("Fixed CPU fixture did not become ready")


def wait_exit(service, code, until):
    while time.monotonic() < until:
        state = service.inspect()
        assert state is not None, "Container disappeared without verified termination"
        if not state["running"]:
            assert state["exit_code"] == code, state
            return
        time.sleep(0.1)
    raise RuntimeError("Owned supervisor did not reach the expected independent termination")


def owned_cleanup(engine, service, image, report, *, expected_owner=None):
    """Acceptance owns these creates, even if implementation validation failed."""
    raw = engine.run("container", "inspect", service.container_id or service.name, check=False)
    if raw.returncode:
        assert not engine.run(
            "container", "ls", "--all", "--filter", "name=^/" + service.name + "$", "--quiet"
        ).stdout.strip()
        return
    record = json.loads(raw.stdout)[0]
    assert record["Name"] == "/" + service.name and record["Image"] == image
    assert record["Config"]["Labels"][PREFIX + "owner"] == (expected_owner or service.owner)
    actual = record["Id"]
    engine.run("container", "kill", actual, check=False)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        state = json.loads(engine.run("container", "inspect", actual).stdout)[0]["State"]
        if not state["Running"] and state["Pid"] == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Owned acceptance container did not stop; retained for recovery")
    engine.run("container", "rm", actual)
    report["cleanup"].append({"container": actual, "removed": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", help="Explicit private rootless Docker Unix socket")
    parser.add_argument("--owner", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.owner:
        owner(options.owner)
        return
    if not options.docker_socket:
        parser.error("--docker-socket is required")
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-container-supervision-" + token)
    out.mkdir(mode=0o700)
    tag = "px-container-supervision-" + token
    report = {
        "scope": "Trusted container supervision with fixed CPU fixtures; no GPU or learner sandbox acceptance",
        "gpu_executed": False,
        "passed": False,
        "checks": [],
        "cleanup": [],
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "Dockerfile",
                "judge/container_contract.py",
                "judge/container_entry.py",
                "judge/container_service.py",
                "judge/gpu_device_lock.py",
                "judge/gpu_command.py",
                "scripts/container_supervision_fixture.py",
                "scripts/container_supervision_acceptance.py",
            )
        },
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    image = None
    try:
        config_path = out / "docker-config"
        config_path.mkdir(mode=0o700)
        with (out / "build.log").open("w") as output:
            subprocess.run(
                [
                    "docker",
                    "--config",
                    str(config_path),
                    "--host",
                    "unix://" + str(engine.path),
                    "build",
                    "--target",
                    "container-supervisor",
                    "--tag",
                    tag,
                    "--iidfile",
                    str(out / "image-id"),
                    "--file",
                    "Dockerfile",
                    ".",
                ],
                cwd=ROOT,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=1200,
            )
        image = (out / "image-id").read_text().strip()
        report["image"] = image
        for scenario in ("deadline-busy", "parent-exit", "owner-exit", "takeover", "replacement"):
            path = out / scenario
            path.mkdir(mode=0o700)
            config = {
                "socket": str(engine.path),
                "image": image,
                "device": "GPU-" + str(uuid.uuid4()),
                "job": str(uuid.uuid4()),
                "reservation": str(uuid.uuid4()),
                "seconds": 14 if scenario == "owner-exit" else 35,
            }
            (path / "config.json").write_text(json.dumps(config))
            if scenario in {"owner-exit", "takeover"}:
                try:
                    with (path / "owner.log").open("w") as output:
                        subprocess.run(
                            [sys.executable, __file__, "--owner", str(path)],
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            check=True,
                            timeout=15,
                        )
                except BaseException:
                    with DeviceLock(path / "locks", config["device"]) as guard:
                        owned_cleanup(
                            engine, service_for(path, guard, engine, image), image, report
                        )
                    raise
            with DeviceLock(path / "locks", config["device"]) as guard:
                service = service_for(path, guard, engine, image)
                active = service
                try:
                    if scenario in {"owner-exit", "takeover"}:
                        previous = json.loads((path / "owner.json").read_text())
                        assert service.inspect()["id"] == previous["container"]
                        assert service.inspect()["running"]
                        status = previous["status"]
                    else:
                        mode = (
                            "busy"
                            if scenario == "deadline-busy"
                            else "exit-parent"
                            if scenario == "parent-exit"
                            else "tree"
                        )
                        status = service.start(
                            [PYTHON, "-I", FIXTURE, mode],
                            deadline=time.monotonic() + (16 if scenario == "deadline-busy" else 35),
                            on_tick=lambda: None,
                        )
                    data = read_fixture(engine, service, "tree")
                    assert set(data["environment"]) <= {
                        "PATH",
                        "LANG",
                        "LC_ALL",
                        "PYTHONDONTWRITEBYTECODE",
                        "TMPDIR",
                    }
                    fixture_hash = (
                        engine.run(
                            "container",
                            "exec",
                            service.container_id,
                            PYTHON,
                            "-I",
                            "-c",
                            f"import hashlib; from pathlib import Path; print(hashlib.sha256(Path('{FIXTURE}').read_bytes()).hexdigest())",
                        )
                        .stdout.decode()
                        .strip()
                    )
                    assert (
                        fixture_hash
                        == report["sources"]["scripts/container_supervision_fixture.py"]
                    )
                    processes = tree(engine, service)
                    if scenario == "deadline-busy":
                        throttling = read_fixture(engine, service, "throttle")
                        numbers = [
                            dict(line.split() for line in throttling[key].splitlines())
                            for key in ("before", "after")
                        ]
                        assert int(numbers[1]["nr_throttled"]) > int(numbers[0]["nr_throttled"])
                        report["cpu_throttling"] = throttling
                    if scenario in {"deadline-busy", "owner-exit"}:
                        wait_exit(service, 124, status["deadline"] + 5)
                    elif scenario == "parent-exit":
                        wait_exit(service, 0, time.monotonic() + 8)
                    else:
                        service.stop()
                    gone(processes)
                    service.remove()
                    report["cleanup"].append({"container": service.container_id, "removed": True})
                    if scenario == "replacement":
                        replacement = service_for(path, guard, engine, image)
                        active = replacement
                        replacement.start(
                            [PYTHON, "-I", FIXTURE, "tree"],
                            deadline=time.monotonic() + 30,
                            on_tick=lambda: None,
                        )
                        try:
                            service.stop()
                        except SandboxUnavailable:
                            assert replacement.inspect()["running"]
                        else:
                            raise RuntimeError("Stale identity incorrectly stopped a replacement")
                        replacement.stop()
                        replacement.remove()
                        report["cleanup"].append(
                            {"container": replacement.container_id, "removed": True}
                        )
                    report["checks"].append(
                        {
                            "name": scenario,
                            "passed": True,
                            "process_tree_gone": True,
                            "limits": status["limits"],
                        }
                    )
                    save()
                    print("PASS " + scenario, flush=True)
                finally:
                    owned_cleanup(engine, active, image, report)
        for scenario in ("foreign-label", "late-start"):
            path = out / scenario
            path.mkdir(mode=0o700)
            with DeviceLock(path / "locks", "GPU-" + str(uuid.uuid4())) as guard:
                service = ContainerService(
                    guard, uuid.uuid4(), uuid.uuid4(), engine=engine, image=image
                )
                original_run = engine.run
                foreign_owner = "acceptance-foreign-" + token

                def altered_run(
                    *args,
                    scenario=scenario,
                    foreign_owner=foreign_owner,
                    actual_owner=service.owner,
                    original_run=original_run,
                    **kwargs,
                ):
                    if scenario == "foreign-label" and args[:2] == ("container", "create"):
                        args = tuple(
                            PREFIX + "owner=" + foreign_owner
                            if item == PREFIX + "owner=" + actual_owner
                            else item
                            for item in args
                        )
                    if scenario == "late-start" and args[:2] == ("container", "start"):
                        time.sleep(5.2)
                    return original_run(*args, **kwargs)

                processes = None
                try:
                    engine.run = altered_run
                    try:
                        service.start(
                            [PYTHON, "-I", FIXTURE, "tree"],
                            deadline=time.monotonic() + 35,
                            on_tick=lambda: None,
                        )
                    except SandboxUnavailable:
                        assert service.container_id
                    else:
                        raise RuntimeError("Invalid supervision transition was accepted")
                    finally:
                        engine.run = original_run
                    if scenario == "foreign-label":
                        # This exact foreign-labelled fixture was created above.
                        # It is not a service discovered on somebody else's behalf.
                        engine.run("container", "start", service.container_id)
                        read_fixture(engine, service, "tree")
                        processes = tree(engine, service)
                        try:
                            service.stop()
                        except SandboxUnavailable:
                            assert json.loads(
                                engine.run("container", "inspect", service.container_id).stdout
                            )[0]["State"]["Running"]
                        else:
                            raise RuntimeError("Foreign-labelled fixture was stopped")
                    else:
                        wait_exit(service, 70, time.monotonic() + 5)
                    report["checks"].append({"name": scenario, "passed": True})
                    print("PASS " + scenario, flush=True)
                finally:
                    engine.run = original_run
                    owned_cleanup(
                        engine,
                        service,
                        image,
                        report,
                        expected_owner=foreign_owner if scenario == "foreign-label" else None,
                    )
                    if processes:
                        gone(processes)
                    save()
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
    finally:
        try:
            if image is not None:
                engine.run("image", "rm", "--no-prune", tag)
                report["owned_image_tag_removed"] = True
        finally:
            save()
    report["passed"] = True
    save()
    print(json.dumps({"passed": report["passed"], "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
