"""Explicit real-WASI acceptance of fitting, uncertainty, diagnostics and transport."""

import json
import os
from pathlib import Path

import pytest

from judge.scientific import (
    evaluate_public,
    load_problem,
    project_public_problem,
    python_weighted_fit_v1,
    validate_problem,
)
from judge.wasi import WasiSandbox

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
        reason="Requires the explicitly enabled pinned real WASI bundle",
    ),
]


@pytest.fixture(scope="module")
def scientific_content():
    if configured := os.environ.get("PRAXIMESH_SCIENCE_CONTENT"):
        path = Path(configured)
        content = load_problem(path)
        assert path.with_name("weighted-line-fit.reference.py").read_text() == content["reference"]
        return content
    package = json.loads((Path(__file__).resolve().parents[1] / "content/starter.json").read_text())
    content = next(
        problem for problem in package["problems"] if problem["id"] == "weighted-line-fit"
    )
    validate_problem(content)
    return content


@pytest.fixture(scope="module")
def scientific_mistakes():
    if configured := os.environ.get("PRAXIMESH_SCIENCE_CONTENT"):
        path = Path(configured).with_name("weighted-line-fit.incorrect.json")
        return json.loads(path.read_text())
    return json.loads(Path(__file__).with_name("incorrect_solutions.json").read_text())[
        "weighted-line-fit"
    ]


@pytest.fixture(scope="module")
def sandbox():
    result = WasiSandbox(os.environ["JUDGE_WASI_BUNDLE"], os.environ["JUDGE_WASI_SHA256"])
    result.preflight()
    return result


def test_actual_fit_reference_including_conditioning_limits_and_statistical_tension(
    scientific_content, sandbox
):
    result = python_weighted_fit_v1(
        scientific_content, scientific_content["reference"], "submit", sandbox
    )
    assert result["verdict"] == "accepted" and result["passed"] == result["total"] == 8
    assert len(result["public_cases"]) == 2
    ordinary, tension = result["public_cases"]
    assert ordinary["model_diagnostic"] == "within_declared_scale"
    assert tension["model_diagnostic"] == "above_declared_scale"
    assert tension["metrics"]["reduced_chi2"] > 100 and tension["verdict"] == "accepted"
    for case in result["public_cases"]:
        assert all(case["checks"].values())
        assert set(case["metrics"]) == {
            "chi2",
            "ndof",
            "reduced_chi2",
            "weighted_normal_residual",
            "covariance_normalized_error",
        }
    serialized = json.dumps(result)
    for case in scientific_content["tests"]:
        assert case["dataset"]["digest"] not in serialized
        if not case["public"]:
            assert case["name"] not in serialized


def test_actual_projected_public_payload_runs_only_public_measurements(scientific_content, sandbox):
    payload = project_public_problem(scientific_content)
    result = evaluate_public(payload, scientific_content["reference"], sandbox)
    assert result["passed"] == result["total"] == 2
    assert "reference" not in payload


@pytest.mark.parametrize("index", range(6))
def test_actual_wrong_weight_and_uncertainty_programs(
    scientific_content, scientific_mistakes, sandbox, index
):
    wrong = scientific_mistakes[index]
    result = python_weighted_fit_v1(scientific_content, wrong["source"], "submit", sandbox)
    assert result["verdict"] == "wrong_answer", wrong["mistake"]


@pytest.mark.parametrize(
    "wire",
    [
        '{"value":{"parameters":[0,0],"parameters":[1,1],"covariance":[[1,0],[0,1]]}}',
        '{"value":{"parameters":[NaN,0],"covariance":[[1,0],[0,1]]}}',
        '{"value":{"parameters":[0,0],"covariance":[[1e999,0],[0,1]]}}',
    ],
)
def test_actual_scientific_raw_transport_rejects_duplicate_nonfinite_and_overflow(
    scientific_content, sandbox, wire
):
    source = f"import os\ndef fit(x,y,sigma):\n os.write(1, {wire.encode()!r})\n os._exit(0)"
    result = python_weighted_fit_v1(scientific_content, source, "run", sandbox)
    assert result["verdict"] == "runtime_error"


def test_actual_scientific_infinite_fit_is_bounded(scientific_content, sandbox):
    result = python_weighted_fit_v1(
        scientific_content, "def fit(x,y,sigma):\n while True: pass", "run", sandbox
    )
    assert result["verdict"] == "time_limit" and result["passed"] == 0
