"""Trusted single-device node check. The guest only stats device metadata."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_contract import PYTHON, STATE  # noqa: E402
from judge.container_devices import nodes, validate_selection  # noqa: E402
from judge.container_gpu_runtime import GPUContainerRuntime  # noqa: E402
from judge.container_transport import readiness  # noqa: E402


def save(name, value):
    temporary = Path(STATE) / (name + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(Path(STATE) / (name + ".json"))


def main():
    report = {"passed": False, "gpu_executed": False, "cuda_initialized": False, "cleanup": []}
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected one fixed device selection")
        selection = validate_selection(json.loads(sys.argv[1]))
        status = readiness()
        manifest = json.loads(Path("/opt/praximesh/driver-manifest.json").read_text())
        runtime = GPUContainerRuntime(
            device_spec=selection,
            driver_manifest=manifest,
            deadline=status["deadline"] - 3,
            on_tick=lambda: None,
            diagnostics=True,
        )
        runtime.create("gpu-nodes", PYTHON, ["-I", "/opt/praximesh/container_gpu_guest.py"])
        with runtime.session(
            "gpu-nodes", mode="submit", on_tick=runtime.tick, on_cleanup=report["cleanup"].append
        ) as session:
            value, _ = session.exchange(
                {"protocol": 1, "operation": "inspect-device-nodes"}, seconds=10
            )
            expected = {key: list(numbers) for key, numbers in nodes(selection).items()}
            assert value["event"] == "device-nodes" and value["nodes"] == expected, value
            assert value["cuda_initialized"] is False and value["uid"] == 0, value
            assert value["root_readonly"] is True and value["supervisor_visible"] is False, value
            report.update(identity=value, runtime_state=runtime.inspect("gpu-nodes"))
            save("gpu-nodes-ready", report)
            while not (Path(STATE) / "observe").exists():
                runtime.tick()
                time.sleep(0.05)
            session.finish()
        assert report["cleanup"] == [{"id": "gpu-nodes", "stopped": True, "removed": True}]
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        log = Path(STATE) / "oci/runtime.log"
        if log.is_file():
            report["runtime_log"] = log.read_text()[-24000:]
        if getattr(error, "diagnostics", None):
            report["diagnostics"] = error.diagnostics.decode(errors="replace")[-4096:]
    finally:
        save("gpu-nodes", report)
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
