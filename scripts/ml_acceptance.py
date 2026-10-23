#!/usr/bin/env python3
"""Replay standalone real-WASI ML acceptance; never execute learner code on the host."""

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge.ml import load_problem, python_ml_linear_v1  # noqa: E402
from judge.wasi import WasiSandbox  # noqa: E402


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--problem", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--incorrect", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("The acceptance receipt path must not already exist.")
    problem = load_problem(args.problem)
    if args.reference.read_text() != problem["reference"]:
        parser.error("The reference source must exactly match the immutable content.")
    sandbox = WasiSandbox(args.bundle, args.bundle_sha256)
    identity = sandbox.preflight()
    result = python_ml_linear_v1(problem, problem["reference"], "submit", sandbox)
    if result["verdict"] != "accepted" or result["passed"] != len(problem["tests"]):
        raise RuntimeError("Reference training did not pass all versioned scenarios.")
    run = python_ml_linear_v1(problem, problem["reference"], "run", sandbox)
    if run["verdict"] != "accepted":
        raise RuntimeError("Public training run did not pass.")
    rejected = []
    for mistake in json.loads(args.incorrect.read_text()):
        wrong = python_ml_linear_v1(problem, mistake["source"], "submit", sandbox)
        if wrong["verdict"] != "wrong_answer":
            raise RuntimeError("A targeted incorrect training program was not rejected.")
        rejected.append(
            {
                "source_sha256": hashlib.sha256(mistake["source"].encode()).hexdigest(),
                "verdict": wrong["verdict"],
            }
        )
    strict_transport = []
    for name, wire in (
        ("duplicate-model-key", '{"value":{"weights":[-123],"weights":[2],"bias":3}}'),
        ("duplicate-envelope-key", '{"value":null,"value":{"weights":[2],"bias":3}}'),
        ("nonfinite-model", '{"value":{"weights":[2],"bias":NaN}}'),
        ("exponent-overflow", '{"value":{"weights":[2],"bias":1e999}}'),
    ):
        source = f"import os\ndef train(features, targets):\n os.write(1, {wire.encode()!r})\n os._exit(0)"
        invalid = python_ml_linear_v1(problem, source, "run", sandbox)
        if invalid["verdict"] != "runtime_error":
            raise RuntimeError("The required strict shared JSON transport is not active.")
        strict_transport.append(name)
    root = Path(__file__).resolve().parents[1]
    receipt = {
        "format": 1,
        "created": datetime.now(UTC).isoformat(),
        "scope": "standalone ML evaluator and real WASI; no Web/schema/worker admission integration asserted",
        "identity": identity,
        "evaluator_sha256": digest_file(root / "judge/ml.py"),
        "acceptance_script_sha256": digest_file(__file__),
        "problem_file_sha256": digest_file(args.problem),
        "problem_canonical_sha256": hashlib.sha256(
            json.dumps(problem, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest(),
        "reference_sha256": digest_file(args.reference),
        "incorrect_file_sha256": digest_file(args.incorrect),
        "reference_submit": result,
        "reference_run": run,
        "incorrect": rejected,
        "strict_transport": strict_transport,
        "policy_integration_required": "judge/ml.py must be included in the registered evaluation policy fingerprint before service admission",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        f"Reference {result['passed']}/{result['total']}; {len(rejected)} incorrect solutions rejected; strict transport passed. Receipt: {args.output}"
    )


if __name__ == "__main__":
    main()
