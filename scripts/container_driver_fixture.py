"""Trusted driver artifact checks; no device injection or CUDA initialization."""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_oci import ContainerRuntime  # noqa: E402
from judge.driver_artifacts import manifest_digest, prepare_request  # noqa: E402
from judge.gpu_contract import compile_request  # noqa: E402
from judge.gpu_session import GPUExecutionError  # noqa: E402


def save(name, value):
    path = Path(STATE) / (name + ".tmp")
    path.write_text(json.dumps(value))
    path.replace(Path(STATE) / (name + ".json"))


def main():
    report = {
        "passed": False,
        "gpu_executed": False,
        "cuda_initialized": False,
        "checks": [],
        "cleanup": [],
        "phase": "supervisor",
    }
    try:
        end = time.monotonic() + 3
        while not (Path(STATE) / "status.json").exists():
            if time.monotonic() >= end:
                raise RuntimeError("Supervisor readiness missing")
            time.sleep(0.02)
        status = json.loads((Path(STATE) / "status.json").read_text())
        runtime = ContainerRuntime(deadline=status["deadline"] - 3, on_tick=lambda: None)
        manifest = json.loads(Path("/opt/praximesh/driver-manifest.json").read_text())
        report["phase"] = "supported-driver-version"
        supported = runtime.command("nvproxy", "list-supported-drivers").stdout.decode()
        versions = re.findall(r"\b[0-9]{3}\.[0-9]{2}\.[0-9]{2}\b", supported)
        if manifest["driver_version"] not in versions:
            raise ValueError("Pinned runsc does not support the exact installed driver")
        report["supported_drivers"] = versions
        cases = json.loads(Path("/opt/praximesh/driver-cases.json").read_text())
        rows = [
            ("load", None),
            *[(item["id"], item) for item in cases],
            ("syntax-error", {**cases[0], "reference": 'extern "C" __global__ void solve( {'}),
            ("recover-syntax", cases[0]),
        ]
        expected = {
            "event": "driver-loaded",
            "manifest_sha256": manifest_digest(manifest),
            "libraries": sorted(item["soname"] for item in manifest["libraries"].values()),
            "gpu_nodes": [],
            "cuda_initialized": False,
        }
        for index, (name, item) in enumerate(rows):
            report["phase"] = name + ":create"
            label = "driver-" + str(index)
            runtime.create(label, PYTHON, ["-I", "/opt/praximesh/judge/container_driver_guest.py"])
            row = {"name": name, "passed": False}
            report["checks"].append(row)
            with runtime.session(
                label, on_cleanup=report["cleanup"].append, mode="submit", on_tick=runtime.tick
            ) as session:
                report["phase"] = name + ":prepare-driver"
                identity, _ = session.exchange(prepare_request(manifest), seconds=15)
                assert identity == expected, identity
                row["identity"] = identity
                if index == 0:
                    save("driver-ready", {"state": runtime.inspect(label), "identity": identity})
                    while not (Path(STATE) / "observe").exists():
                        runtime.tick()
                        time.sleep(0.05)
                if item is not None:
                    report["phase"] = name + ":compile"
                    try:
                        value, _ = session.exchange(
                            {
                                **compile_request(item["reference"], item["operation"]),
                                "architecture": [8, 6],
                            },
                            seconds=15,
                        )
                    except GPUExecutionError as error:
                        if name != "syntax-error" or error.verdict != "syntax_error":
                            raise
                        row["status"] = error.verdict
                    else:
                        assert name != "syntax-error" and value["event"] == "compiled", value
                        assert value["architecture"] == [8, 6] and value["ptx_bytes"] > 0, value
                        assert re.fullmatch(r"[0-9a-f]{64}", value["ptx_sha256"]), value
                        row["compilation"] = value
                        session.finish()
                else:
                    session.finish()
            row["passed"] = True
        assert report["cleanup"] == [
            {"id": "driver-" + str(i), "stopped": True, "removed": True} for i in range(len(rows))
        ]
        report["passed"] = True
        report["phase"] = "complete"
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        if getattr(error, "diagnostics", None):
            report["diagnostics"] = error.diagnostics.decode(errors="replace")[-4096:]
    finally:
        save("driver", report)
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
