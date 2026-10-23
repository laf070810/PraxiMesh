# Known-error weighted scientific fitting

This CPU evaluator validates a fitted physical model and its uncertainty, using
Python's basic standard library in the existing capability-limited WASI guest.
It does not load native learner artifacts, use GPU, or trust self-reported fit
scores. A correct implementation may produce a fit with substantial statistical
tension; correctness and the model diagnostic are separate outputs.

## Contract and numerical domain

The immutable owner content has `evaluator="python-weighted-fit-v1"`,
`language="python"`, `function="fit"`, and
`uncertainty_model="known-independent-sigma-v1"`. Each of 2–8 scenarios contains
exactly `name`, `public`, `observations={x,y,sigma}`, and
`dataset={id,revision,seed,digest}`. Formal content must contain both public and
hidden scenarios. The guest receives only the three observation vectors.

The learner returns exactly:

```json
{"parameters": ["intercept", "slope"],
 "covariance": [["C00", "C01"], ["C10", "C11"]]}
```

The strings above are placeholders for finite JSON numbers. Parameters have
absolute value ≤10^8; covariance entries ≤10^12. Booleans, extra keys, nonfinite
numbers, wrong dimensions and raw score fields are rejected. Shared strict JSON
decoding must reject duplicate keys and exponent overflow before producing the
model dict. These decoder dependencies are supplied by the stdio runtime change.

Each vector has 4–64 entries; |x| and |y|≤10^4, sigma∈[0.01,100], with
max(sigma)/min(sigma)≤100 (weight ratio≤10^4). Repeated x measurements are allowed,
but weighted variance Vx must be ≥10^-6, and |weighted mean x|/sqrt(Vx)≤1000.
These explicit bounds prevent unidentifiable slopes, vanishing spread and
numerically unstable intercept extrapolation. All owner cases are validated
before executing any case, including when the server runs public examples.
Invalid content is a platform error, never a learner wrong answer. Content is
at most 1 MB and source retains the 20,000-character limit.

Dataset digest is SHA256 of compact sorted UTF-8 JSON
`{"dataset": {id,revision,seed}, "observations": {x,y,sigma}}`, using
`ensure_ascii=False`, `allow_nan=False`, `separators=(",", ":")`. The digest itself
is excluded. ID is a lowercase slug, revision a positive 31-bit integer, and seed
an unsigned 32-bit integer; names and (id,revision) pairs are unique. Actual data,
not a claimed seed, are scored. The enclosing ProblemVersion binds the contract.

## Independent host scoring

The host computes w=1/sigma², weighted centers, Sxx=Σw(x−x̄)², and
Sxy=Σw(x−x̄)(y−ȳ). Its oracle is b=Sxy/Sxx, a=ȳ−b*x̄ and:

```text
C00 = 1/Σw + x̄²/Sxx
C01 = C10 = −x̄/Sxx
C11 = 1/Sxx
```

Sigma is a known absolute independent measurement uncertainty. The covariance
is **not** multiplied by χ²/(n−2). This distinction follows the weighted versus
unknown-error covariance definitions in the [GNU GSL linear least-squares
documentation](https://www.gnu.org/software/gsl/doc/html/lls.html). Uncertain or
estimated weights need a different inference contract; the [NIST treatment of
weighted least squares](https://itl.nist.gov/div898/handbook/pmd/section1/pmd143.htm)
explains why this assumption matters. No claim of general confidence coverage
is made by this finite exercise.

Parameter correctness uses `math.isclose` with absolute tolerance 10^-8 and
relative tolerance 10^-7. Covariance entry tolerance is
`10^-8*sqrt(Cii*Cjj) + 10^-7*abs(Cij)` using **oracle** C. The scaled absolute
term respects units and prevents a conventional fixed absolute tolerance from
accepting arbitrarily tiny, incorrect variances. Roundoff asymmetry must be at
most `10^-8*sqrt(C00*C11)`, and the symmetric covariance must be positive definite.

The host also computes residuals r=a+b*x−y. It requires both whitened normal
equation residuals to be at most 10^-5:

```text
|Σw*r| / sqrt(Σw)
|Σw*(x−x̄)*r| / sqrt(Sxx)
```

This prevents loose relative tolerance on a large intercept from accepting an
incorrect predicted line. The score is based on all three independent checks:
parameters, covariance and stationarity. The numerical host oracle is checked
against a separate exact-Fraction implementation of the raw normal equations,
including large offsets, small spread and extreme weights.

## Feedback, model diagnosis and privacy

Results retain verdict/passed/total/elapsed_ms/public_cases. Explicitly public
scenarios include boolean `checks={parameters,covariance,stationarity}`, and
host-derived `metrics={chi2,ndof,reduced_chi2,weighted_normal_residual,
covariance_normalized_error}`. The last value is the maximum covariance error
divided by sqrt(Cii*Cjj). No raw returned parameters or covariance are stored.

`model_diagnostic` is `not_assessed` if any correctness check fails. For a correct
fit it is `below_declared_scale` when reduced χ²<0.25, `above_declared_scale` when
reduced χ²>3, otherwise `within_declared_scale`. These explicitly declared scale
guides are **not p-values, confidence levels, proof of a model, or causes of
rejection**. Values outside the guide motivate review of model/error assumptions;
they do not identify which assumption failed. A high-χ² correct fit is accepted.

Hidden measurements necessarily enter the isolated guest to be fitted. They do
not return through feedback, exception text, result export or public showcase;
hidden names, dataset metadata, fit values and per-case diagnostics are omitted.
The existing aggregate passed count retains its existing visibility behavior.
Run never executes hidden scenarios. Actual sandbox failure categories survive;
platform failures and lost leases are not scored as learner errors. elapsed_ms
is diagnostic, not a fair speed ranking on the shared host.

The assessment marker is
`{kind:"known-independent-sigma-v1", metric_version:"weighted-line-diagnostics-v1",
visibility:"public-cases-only", diagnostic_is_correctness:false}`.

## Public local evaluation

`project_public_problem(full_version)` explicitly copies only
`evaluator/language/function/uncertainty_model/tests`; each selected case has
only `name/public/observations/dataset`, with the nested exact fields above.
It never copies reference source, hidden cases or arbitrary owner metadata.
Presentation metadata belongs outside this execution payload.

`validate_public_problem` accepts only that exact allowlist, 1–8 cases, all
`public=True`. `evaluate_public(payload, source, sandbox, on_case=...)` has no
mode argument and always runs public examples. `load_public_problem` uses the
same bounded strict file decoder. The formal `python_weighted_fit_v1` always
requires the complete public+hidden contract; there is no content-controlled
flag that relaxes formal validation.

## Content, execution evidence and integration

The standalone eight-scenario content includes ordinary heteroscedastic data,
curved model mismatch, sigma rescaling, x-unit rescaling, large x offset, small
spread near the accepted boundary, maximal permitted weight ratio, and the
minimal sample count. A trusted deterministic generator reproduces all stored
data/hashes. Its finite Gaussian-shaped draws are test fixtures, not a coverage
study. The correct guest source is executed only in WASI; six targeted programs
test wrong weights, covariance scaling, omitted correlation, negative variance,
and forged χ². Real tests also cover raw wire attacks and infinite computation.

Parent integration must register the evaluator, include `judge/scientific.py`
and the shared decoder in the policy fingerprint, extend owner content schema,
renew admission, and display public checks and diagnostics as separate concepts.
The independent delivery does not change registry/schema/shared code or attest
to Web→worker integration. API/local CLI can use the public projection helpers;
exports and explicit public results must retain this same safe feedback shape.
`scripts/scientific_acceptance.py` generates an exclusive receipt from real WASI
execution and labels its standalone scope. It never runs learner code on host.
