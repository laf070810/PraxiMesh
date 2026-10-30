"""Controller checks for reviewed CUDA probe fixtures only."""

import json
from pathlib import Path

from judge import gpu_artifacts
from judge.gpu_artifacts import IMAGE as IMAGE
from judge.gpu_artifacts import verify_image_archive as verify_image_archive
from judge.gpu_contract import case_request, compile_request, output_matches, reference_values
from judge.gpu_probes import correctness_fixtures, fault_fixtures

COMPARISON = {"kind": "float", "absolute": 1e-5, "relative": 1e-5}


def prepare_artifacts(base, archive, root):
    identity = gpu_artifacts.prepare_kernel_artifacts(base, archive, root)
    identity.update(
        fixture_sha256=gpu_artifacts.sha256(root / "judge/gpu_probes.py"),
        probe_sha256=gpu_artifacts.sha256(Path(__file__)),
    )
    return identity


def container_options(base, *, diagnostics=True):
    return gpu_artifacts.container_options(base, diagnostics=diagnostics)


def fixture_request(fixture):
    requests = [compile_request(fixture["source"], fixture["operation"])]
    requests += [
        case_request(fixture["operation"], {"inputs": inputs}) for inputs in fixture["inputs"]
    ]
    return "".join(json.dumps(request, allow_nan=False) + "\n" for request in requests)


def valid_compiled_frame(frame):
    return (
        type(frame) is dict
        and set(frame) == {"event", "architecture"}
        and frame["event"] == "compiled"
        and type(frame["architecture"]) is list
        and len(frame["architecture"]) == 2
        and all(type(value) is int and value >= 0 for value in frame["architecture"])
    )


def verify_output(fixture, output):
    frames = [json.loads(line) for line in output.splitlines()]
    if fixture["exit_code"]:
        if fixture.get("compiled"):
            if len(frames) != 1 or not valid_compiled_frame(frames[0]):
                raise ValueError("Runtime fault must follow compilation and emit no result")
        elif frames:
            raise ValueError("Failed compilation or entrypoint must not emit result frames")
        return
    if len(frames) != len(fixture["inputs"]) + 1 or not valid_compiled_frame(frames[0]):
        raise ValueError("Invalid CUDA compilation/result frames")
    correct = []
    for inputs, frame in zip(fixture["inputs"], frames[1:], strict=True):
        if type(frame) is not dict or set(frame) != {"values"}:
            raise ValueError("Only output values are accepted from the GPU harness")
        expected = reference_values(fixture["operation"], inputs)
        correct.append(output_matches(frame["values"], expected, COMPARISON))
    expected_results = all(correct) if fixture["correct"] else not any(correct)
    if not correct or not expected_results:
        raise ValueError("Reference or incorrect fixture did not match controller expectation")


def verify_fixtures(run, report, save, *, faults=False):
    report["kernel_probes"] = []
    for fixture in fault_fixtures() if faults else correctness_fixtures():
        output = run(
            fixture["name"],
            "python",
            ["-B", "-m", "judge.gpu_runtime"],
            stdin=fixture_request(fixture),
            expected_exit=fixture["exit_code"],
            timeout=fixture.get("timeout", 60),
            expected_timeout=fixture.get("expected_timeout", False),
        )
        verify_output(fixture, output)
        report["kernel_probes"].append({"name": fixture["name"], "passed": True})
        save()
        print(json.dumps({"phase": "kernel_probe_passed", "name": fixture["name"]}), flush=True)
