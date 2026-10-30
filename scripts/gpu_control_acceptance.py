"""Exercise owned task services using fixed CPU fixtures; never access a GPU."""

import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_device_lock import DeviceLock  # noqa: E402
from judge.gpu_service import OwnedJobService  # noqa: E402
from judge.sandbox import SandboxUnavailable  # noqa: E402

FIXTURE = ROOT / "scripts/gpu_control_fixture.py"


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("CPU service fixture did not reach its expected state")
        time.sleep(0.05)


def stopped_process(record):
    try:
        fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return True
    return fields[19] != record["start_ticks"] or fields[0] == "Z"


def main():
    if len(sys.argv) != 1:
        raise ValueError("This fixed acceptance has no GPU or executable argument")
    token = uuid.uuid4().hex[:12]
    out = ROOT / "test-results" / ("px-gpu-control-" + token)
    out.mkdir(mode=0o700, parents=True)
    report = {
        "scope": "CPU-only service control acceptance; no GPU, sandbox or receipt proof",
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                "judge/gpu_service.py",
                "core/gpu_control.py",
                "scripts/gpu_service_entry.py",
                "scripts/gpu_control_fixture.py",
                "scripts/gpu_control_acceptance.py",
            ]
        },
        "checks": [],
        "cleanup": [],
        "passed": False,
    }

    def save():
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    print(json.dumps({"report": str(out / "result.json")}), flush=True)
    try:
        for scenario in ("deadline", "parent-exit", "owner-exit", "foreign-description"):
            path = out / scenario
            path.mkdir(mode=0o700)
            # A synthetic UUID names only this private file lock; no NVML/CUDA call.
            config = {
                "device": "GPU-" + str(uuid.uuid4()),
                "job": str(uuid.uuid4()),
                "reservation": str(uuid.uuid4()),
            }
            (path / "config.json").write_text(json.dumps(config))
            if scenario == "owner-exit":
                # The fixture's controller exits; the replacement must recover
                # its service using only the persistent reservation identity.
                owner = subprocess.run(
                    [sys.executable, str(FIXTURE), "owner", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                (path / "owner.log").write_text(owner.stdout + owner.stderr)
            with DeviceLock(path / "locks", config["device"]) as guard:
                service = OwnedJobService(guard, config["job"], config["reservation"])
                foreign_created = False
                started = time.monotonic()
                try:
                    if scenario == "foreign-description":
                        command = [
                            "systemd-run",
                            "--user",
                            "--quiet",
                            "--collect",
                            "--unit=" + service.unit,
                            "--description=PraxiMesh fixed foreign-service acceptance " + token,
                            "--property=RuntimeMaxSec=20",
                            "--property=KillMode=control-group",
                            "--property=StandardOutput=null",
                            "--property=StandardError=null",
                            "/usr/bin/sleep",
                            "30",
                        ]
                        foreign_created = True  # Also clean up a partially successful launch.
                        subprocess.run(command, capture_output=True, check=True, timeout=5)
                        try:
                            service.stop()
                        except SandboxUnavailable:
                            active = subprocess.run(
                                ["systemctl", "--user", "is-active", service.unit],
                                capture_output=True,
                                text=True,
                                check=True,
                                timeout=3,
                            )
                            assert active.stdout.strip() == "active"
                        else:
                            raise RuntimeError("An unrelated service was incorrectly stopped")
                        report["checks"].append(
                            {
                                "name": scenario,
                                "refused": True,
                                "unrelated_service_still_active": True,
                            }
                        )
                        continue
                    if scenario != "owner-exit":
                        service.start(
                            [sys.executable, str(FIXTURE), "tree", str(path)],
                            deadline=time.monotonic() + (14 if scenario == "deadline" else 60),
                            on_tick=lambda: None,
                        )
                    else:
                        assert owner.returncode == 0, "The fixed owner did not start its service"
                        assert (
                            service.inspect()["InvocationID"]
                            == json.loads(owner.stdout)["invocation"]
                        )
                        assert service.populated(), "Owner exit unexpectedly stopped the service"
                    limits = service.limits()
                    wait_for(
                        lambda path=path: (
                            (path / "leaf.json").exists() and (path / "tree.json").exists()
                        )
                    )
                    records = [
                        json.loads((path / (mode + ".json")).read_text())
                        for mode in ("tree", "leaf")
                    ]
                    if scenario == "parent-exit":
                        (path / "exit-parent").touch()
                    if scenario == "owner-exit":
                        service.stop()
                    # No stop request in the deadline and parent-exit scenarios.
                    wait_for(lambda service=service: not service.populated())
                    assert all(stopped_process(record) for record in records)
                    elapsed = time.monotonic() - started
                    report["checks"].append(
                        {
                            "name": scenario,
                            "unit": service.unit,
                            "elapsed_seconds": elapsed,
                            "parent_and_child_stopped": True,
                            "cgroup_empty": True,
                            "independent_deadline_seconds": service.runtime_limit,
                            "parent_cgroup_limits": limits,
                        }
                    )
                finally:
                    if foreign_created:
                        # This deliberately mismatched fixture belongs to this
                        # acceptance, so its own creator cleans it up explicitly.
                        subprocess.run(
                            ["systemctl", "--user", "stop", service.unit],
                            capture_output=True,
                            check=True,
                            timeout=8,
                        )
                        wait_for(lambda service=service: not service.populated())
                        cleaned = {"unit": service.unit, "stopped": True, "cgroup_empty": True}
                    else:
                        cleaned = service.stop()
                    report["cleanup"].append(cleaned)
                    save()
                print(json.dumps({"passed": scenario}), flush=True)
        report["passed"] = True
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
