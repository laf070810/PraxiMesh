"""Controller comparisons must reject missing, forged and misclassified results."""

import json

import pytest

from judge.gpu_contract import reference_values
from judge.gpu_probes import correctness_fixtures
from scripts.gpu_kernel_probe import fixture_request, prepare_artifacts, verify_output


def reference_fixture():
    return correctness_fixtures()[0]


def output_frames(fixture):
    return [
        {"event": "compiled", "architecture": [8, 6]},
        *[
            {"values": reference_values(fixture["operation"], inputs)}
            for inputs in fixture["inputs"]
        ],
    ]


def serialize(frames):
    return "".join(json.dumps(frame) + "\n" for frame in frames)


def test_unknown_compiler_is_rejected_without_extracting_or_copying_harness(tmp_path):
    archive = tmp_path / "untrusted.tar.xz"
    archive.write_bytes(b"not an approved compiler")
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_artifacts(tmp_path, archive, tmp_path / "not-used")
    assert sorted(path.name for path in tmp_path.iterdir()) == [archive.name]


def test_fixture_transport_contains_only_source_and_inputs():
    fixture = dict(reference_fixture(), expected="protected", name="private-label")
    frames = [json.loads(line) for line in fixture_request(fixture).splitlines()]
    assert set(frames[0]) == {"protocol", "source", "operation"}
    assert len(frames[1:]) == len(fixture["inputs"])
    assert all(set(frame) == {"protocol", "inputs"} for frame in frames[1:])
    assert "protected" not in serialize(frames) and "private-label" not in serialize(frames)


@pytest.mark.parametrize("defect", ["missing", "extra", "verdict", "wrong", "nonfinite", "bool"])
def test_untrusted_frames_cannot_claim_a_successful_probe(defect):
    fixture = reference_fixture()
    frames = output_frames(fixture)
    if defect == "missing":
        frames.pop()
    elif defect == "extra":
        frames.append({"values": []})
    elif defect == "verdict":
        frames[2] = {"verdict": "accepted", "score": 100}
    elif defect == "wrong":
        frames[2]["values"][0] += 1
    elif defect == "nonfinite":
        frames[2]["values"][0] = float("nan")
    else:
        frames[0]["architecture"] = [True, 6]
    with pytest.raises(ValueError):
        verify_output(fixture, serialize(frames))


def test_wrong_implementation_passing_correctness_fails_probe():
    fixture = dict(reference_fixture(), correct=False)
    with pytest.raises(ValueError, match="controller expectation"):
        verify_output(fixture, serialize(output_frames(fixture)))


def test_compilation_failure_cannot_also_emit_success():
    fixture = next(value for value in correctness_fixtures() if value["name"] == "compile-error")
    verify_output(fixture, "")
    with pytest.raises(ValueError, match="must not emit"):
        verify_output(fixture, '{"event": "compiled", "architecture": [8, 6]}\n')


def test_valid_controller_comparison_is_independent_of_guest_verdicts():
    fixture = reference_fixture()
    verify_output(fixture, serialize(output_frames(fixture)))
