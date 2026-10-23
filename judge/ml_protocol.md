# Bounded CPU model training protocol

`python-ml-linear-v1` is a reviewed host evaluator. It requests an actual fitted
affine model from a sandbox and independently predicts held-out data. It does not
execute learner source, deserialize models, or trust learner-reported scores.
The first content item is `train-linear-model`, provided separately for owner
import. It uses Python's basic standard library in the existing WASI environment;
it makes no claim about GPU, PyTorch, NumPy, arbitrary ML architectures, or a
particular optimizer.

## Contract

Common immutable ProblemVersion fields remain the content owner's concern. This
module additionally requires `language="python"`,
`evaluator="python-ml-linear-v1"`, `model_kind="linear-regression-v1"`, and an
ASCII Python identifier in `function` (the supplied task uses `train`). The learner
implements `train(features, targets)` and returns exactly
`{"weights": [number, ...], "bias": number}`.

Each of 2–8 scenarios has exactly these keys:

```json
{
  "name": "public scenario name or private identifier",
  "public": true,
  "train": {"features": [[1]], "targets": [3]},
  "holdout": {"features": [[2]], "targets": [5]},
  "thresholds": {"mse": 0.001, "mae": 0.03},
  "dataset": {"id": "versioned-data", "revision": 1, "seed": 11, "digest": "64 lowercase hex"}
}
```

The shortened matrices above illustrate the structure; valid splits have 4–128
rows, 1–8 columns, and matching target counts and dimensions. The scenario set
must contain both public and hidden scenarios. Feature rows must be unique
within and across the two splits. All feature/target/model values are finite
JSON numbers of absolute value at most 1,000,000; bool is rejected. Thresholds
are finite nonnegative numbers at most 10^12. Dataset IDs are lowercase slugs,
revisions are positive 31-bit integers, and seeds are unsigned 32-bit integers;
bool is rejected in all these integer fields. Dataset (id, revision) pairs and
scenario names are unique. A complete UTF-8 JSON content item is at most 1 MB.
Source has the existing 20,000 character limit. No package upload, native model,
pickle, external file, or executable artifact is accepted.

`dataset.digest` is SHA256 of UTF-8 canonical JSON for this object:

```text
{"dataset": {"id": ..., "revision": ..., "seed": ...},
 "train": {...}, "holdout": {...}}
```

Canonicalization uses `sort_keys=True`, `ensure_ascii=False`, `allow_nan=False`,
and `separators=(",", ":")`. It excludes the digest itself. Actual rows and
labels are bound, not only the seed. Seed records provenance and is not accepted
from the learner as evidence. Thresholds and the complete contract remain bound
by the enclosing immutable ProblemVersion digest (the existing content digest
has different whitespace; do not confuse the two hashes).

## Scoring and privacy

All scenarios, including hidden ones, are validated before the first execution.
Each sandbox invocation receives only training features and targets. It receives
no holdout features/labels, expected model, threshold, dataset metadata, or name.
The host computes predictions with the validated coefficients, MSE and MAE from
the actual held-out residuals. Both error thresholds must pass. Invalid models
are `wrong_answer`; actual sandbox failures retain their failure category.
Invalid owner content or unavailable execution is a platform error and must
remain `internal_error` at the worker boundary. Lease cancellation propagates
before the next scenario. Run executes only public scenarios. Each invocation
uses a fresh guest and the existing per-call fuel/memory/output/wall-time limits;
the protocol is bounded to eight calls, and introduces no timing-based ranking.

Results retain verdict/passed/total/elapsed_ms/public_cases, adding
`assessment={kind:"linear-regression-v1", metric_version:"mse-mae-v1",
visibility:"public-cases-only"}`. Only explicitly public scenarios include
host-computed `metrics={mse,mae}` and `thresholds={mse,mae}`. No raw returned model,
training/holdout rows, hidden per-case metrics, hidden names or dataset IDs are
stored in feedback. The existing passed count can reveal a failure position as
in other evaluators; it is not a private aggregate model score. `elapsed_ms` is
diagnostic and cannot be interpreted as a fair speed rank on the shared host.

The shared sandbox **must** use strict JSON decoding before constructing its
Execution object: reject duplicate keys at all levels, NaN/Infinity, exponent
overflow, excessive depth/node count, trailing output, and malformed envelopes.
A Python dict validator cannot recover duplicate keys that a permissive decoder
has already erased. The shared `judge.stdio.parse_output` implementation belongs
to the runtime increment, not this independent module.

## First dataset and validation boundary

The standalone content contains six deterministic scenarios: two public and four
hidden, covering one/eight dimensions, nonzero intercept, negative coefficients,
shifted/scaled inputs, and two noisy scenarios. Noisy labels use bounded fixed
LCG32 noise, with MSE≤0.003 and MAE≤0.06; noiseless thresholds are MSE≤10^-12 and
MAE≤10^-6. The reference fits a centered least-squares system with pivoting using
only the training split. This checks real fitting and prediction on unseen rows;
it does not establish population risk, mastery, or statistical confidence across
arbitrary datasets.

Unit tests validate host scoring, privacy and malformed contracts. Separate
explicitly enabled WASI tests execute the reference, six targeted incorrect
programs, resource failures, inaccessible host-heldout data, and raw forged
transport. `scripts/ml_acceptance.py` produces an exclusive receipt from actual
execution and refuses to attest if the strict shared transport is unavailable.
It reports a standalone scope, not worker admission or Web integration.

## Parent integration checklist

1. Register the evaluator in the reviewed registry and add `judge/ml.py` plus
   relevant shared decoder files to `policy_digest`; new code requires renewed
   admission. No ordinary Docker/host execution fallback is permitted.
2. Extend the content schema and owner import validation using
   `validate_problem`. Keep the existing immutable revision and digest behavior.
3. Project only explicitly public training/holdout data for public examples. Do
   not reuse a generic serializer that assumes function `args/expected` or dumps
   all `tests`; hidden rows and reference source stay private.
4. Display the task's model return contract and host-computed public MSE/MAE in
   Web/API/CLI. Do not silently remove these metrics or display self-reported
   values. Exports and explicit showcases use the same safe result projection.
5. Exercise real authenticated submit→worker→result, Run separation, lease/error
   handling, private export/deletion, and version/fingerprint grouping before
   claiming service acceptance. An AC is evidence for this version's finite
   scenarios, not general ML mastery.
