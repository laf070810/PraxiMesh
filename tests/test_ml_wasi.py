"""Real WASI ML acceptance; skips never constitute execution evidence."""

import json
import os
from pathlib import Path

import pytest

from judge.ml import load_problem, python_ml_linear_v1, validate_problem
from judge.wasi import WasiSandbox

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
        reason="Requires the explicitly enabled pinned real WASI bundle",
    ),
]


@pytest.fixture(scope="module")
def ml_content():
    if configured := os.environ.get("PRAXIMESH_ML_CONTENT"):
        path = Path(configured)
        content = load_problem(path)
        assert path.with_name("train-linear-model.reference.py").read_text() == content["reference"]
        return content
    package = json.loads((Path(__file__).resolve().parents[1] / "content/starter.json").read_text())
    content = next(
        problem for problem in package["problems"] if problem["id"] == "train-linear-model"
    )
    validate_problem(content)
    return content


@pytest.fixture(scope="module")
def ml_mistakes():
    if configured := os.environ.get("PRAXIMESH_ML_CONTENT"):
        path = Path(configured).with_name("train-linear-model.incorrect.json")
        return json.loads(path.read_text())
    return json.loads(Path(__file__).with_name("incorrect_solutions.json").read_text())[
        "train-linear-model"
    ]


@pytest.fixture(scope="module")
def sandbox():
    sandbox = WasiSandbox(os.environ["JUDGE_WASI_BUNDLE"], os.environ["JUDGE_WASI_SHA256"])
    sandbox.preflight()
    return sandbox


def test_actual_full_training_reference_and_noisy_generalization(ml_content, sandbox):
    result = python_ml_linear_v1(ml_content, ml_content["reference"], "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == result["total"] == 6
    public = result["public_cases"]
    assert len(public) == 2
    assert 0 < public[1]["metrics"]["mse"] <= public[1]["thresholds"]["mse"]
    assert 0 < public[1]["metrics"]["mae"] <= public[1]["thresholds"]["mae"]
    serialized = json.dumps(result)
    for case in ml_content["tests"]:
        assert case["dataset"]["digest"] not in serialized
        if not case["public"]:
            assert case["name"] not in serialized
    assert "weights" not in serialized and "targets" not in serialized
    run = python_ml_linear_v1(ml_content, ml_content["reference"], "run", sandbox)
    assert run["passed"] == run["total"] == 2


@pytest.mark.parametrize("index", range(6))
def test_actual_targeted_training_mistakes(ml_content, ml_mistakes, sandbox, index):
    wrong = ml_mistakes[index]
    result = python_ml_linear_v1(ml_content, wrong["source"], "submit", sandbox)
    assert result["verdict"] == "wrong_answer", wrong["mistake"]
    assert result["passed"] < result["total"]


@pytest.mark.parametrize(
    "source,verdict",
    [
        ("def train(features, targets):\n while True: pass", "time_limit"),
        ("def train(features, targets):\n print('x' * 50000)\n return {}", "output_limit"),
        ("def train(features, targets):\n raise ValueError(repr(targets))", "runtime_error"),
    ],
)
def test_actual_training_failures_are_bounded_and_redacted(ml_content, sandbox, source, verdict):
    result = python_ml_linear_v1(ml_content, source, "submit", sandbox)
    assert result["verdict"] == verdict
    assert result["passed"] == 0
    assert set(result["public_cases"][0]) == {"name", "verdict", "label"}


@pytest.mark.parametrize(
    "wire",
    [
        '{"value":{"weights":[-123],"weights":[2],"bias":3}}',
        '{"value":null,"value":{"weights":[2],"bias":3}}',
        '{"value":{"weights":[2],"bias":NaN}}',
        '{"value":{"weights":[2],"bias":1e999}}',
    ],
)
def test_actual_forged_transport_rejects_duplicate_or_nonfinite_model(ml_content, sandbox, wire):
    # Source is still executed only in WASI. It bypasses the guest JSON writer,
    # exercising the trusted host decoder rather than a pre-decoded Python dict.
    source = (
        f"import os\ndef train(features, targets):\n os.write(1, {wire.encode()!r})\n os._exit(0)"
    )
    result = python_ml_linear_v1(ml_content, source, "run", sandbox)
    assert result["verdict"] == "runtime_error"
    assert result["passed"] == 0


def test_actual_guest_cannot_read_heldout_canary_from_host(ml_content, sandbox, tmp_path):
    canary = tmp_path / "controller-heldout.json"
    canary.write_text(json.dumps(ml_content["tests"][0]["holdout"]))
    source = (
        "def train(features, targets):\n"
        " try:\n"
        f"  open({str(canary)!r}).read()\n"
        "  return {}\n"
        " except OSError:\n"
        '  return {"weights": [2], "bias": 3}\n'
    )
    result = python_ml_linear_v1(ml_content, source, "run", sandbox)
    assert result["public_cases"][0]["verdict"] == "accepted"
    assert json.loads(canary.read_text()) == ml_content["tests"][0]["holdout"]
