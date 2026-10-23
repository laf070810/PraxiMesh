# Bounded Python tool agents

`python-agent-tools-v1` evaluates a Python `act(observation, state)` function
against a host-owned ledger. The evaluator executes every learner turn in a
fresh WASI instance. It has no network tools, shell tools, arbitrary imports on
the host, or external account side effects. This is a small CPU agent task with
real state transitions in the assessment's private environment; it does not
claim to evaluate an unrestricted assistant or an external service integration.

## Problem and execution contract

The evaluator requires `language: "python"`, `function: "act"`, and exactly:

```json
{"kind":"approved-ledger-v1","resource_profile":"wasi-agent-v1"}
```

in `assessment`. Formal problems contain both public and hidden episodes, up to
eight total. Each episode has exactly `name`, `public`, and `environment`; its
environment has exactly `records`, a list of one to four records. Each record
has exactly `id`, `amount`, and `approved`:

* IDs are unique within the episode and match `[A-Za-z0-9_-]{1,32}`.
* Amounts are integers from −100000 to 100000 in a declared currency's minor
  units. The assessment performs integer arithmetic and no currency conversion.
* Approval is a boolean. A boolean is never accepted as an amount or count.

Each call receives `observation = {task, last_result, remaining_turns}` and the
previous returned state. Initially `last_result` and state are `null`. Learner
source is limited to 20000 characters. The complete JSON request is limited to
131072 bytes, including worst-case source escaping and the bounded observation
and state.

The function returns exactly `{tool, args, state}`. The state is untrusted JSON
data, limited to 1024 UTF-8 bytes, depth six, and 128 nodes. Strings have at most
128 characters; object keys have at most 32. Numbers must be finite and have
absolute value at most one billion. The state cannot grant permissions, replace
the environment, supply a score, or declare successful completion.

| Tool | Exact arguments | Host action | Cost |
| --- | --- | --- | --- |
| `list_records` | `{}` | Returns the episode's record IDs and records that listing occurred. | 1 |
| `read_record` | `{"id": "..."}` | Requires listing first and an existing ID; returns the actual record and marks it read. | 1 |
| `submit_report` | `{"total": integer, "count": integer}` | Requires every record to have been read; stores the report and checks it against the host-computed approved sum and count. | 2 |

A report's total must be between −400000 and 400000 and its count between zero
and four. Submission is terminal whether correct or incorrect. Repeated calls
consume budget. Unknown tools, extra arguments, missing observations, invalid
state, and fabricated success fields are wrong answers. A correct four-record
episode needs six guest calls and tool cost seven.

The host creates each trajectory entry after validating and applying the tool:
`{turn, tool, args, result, cost}`. It never stores the learner's raw state or
source in this trajectory. Returned public feedback includes only public
episodes; hidden episode names, IDs, records, and trajectories are omitted.

## Resource profile

`judge/agent_policy.py` is the server-owned versioned profile. Its canonical JSON
SHA-256 is included in every public episode's metrics. Content can select the
known profile but cannot change its limits. Integrators must include the Agent
policy, driver, runtime, evaluator, and probes in the worker's critical source
identity and successful combined sandbox receipt before enabling this evaluator.

| Limit | Per guest call | Per episode |
| --- | --- | --- |
| Guest calls | One fresh instance | At most 6 |
| Wall time | 5 seconds | 20 seconds |
| Driver CPU | Native soft limit 1 second, hard limit 2 seconds | 6 seconds, accumulated across children |
| Wasmtime fuel | 2500000000 | At most 15000000000 reserved |
| Combined guest stdout/stderr | 4096 bytes | At most 24576 bytes reserved |
| Tool cost | 1 or 2 | At most 7 |

Fuel and output reservations are conservative maxima, not measurements of fuel
consumption or actual output. CPU metrics are actual user plus system CPU from
`wait4` for the dedicated trusted driver, including interpreter startup and
supervision inside that process. They are not pure learner-instruction timings.
The existing WASI engine also enforces its 128 MiB linear-memory and 1536 MiB
native-address-space caps, file descriptor cap, empty environment, fixed CPU
affinity, and read-only interpreter files. The Agent driver preserves those
capabilities and uses the verified Python runtime from the joint bundle.

The supervisor checks cumulative CPU through `/proc/<own-child-pid>/stat` at a
nominal 20 ms interval, then collects exact final usage with `wait4`. It never
reads another user's process and never calls `Popen.wait` or `poll` first, which
would discard the usage record. CPU accounting is quantized while the process
is alive. Scheduling and termination can extend a limit by a small amount; this
is not a zero-overshoot guarantee. Native CPU limits and the driver wall timer
remain independent backstops. Termination kills only the created process group
and reaps the child; supervisor death also kills the driver through the existing
Linux parent-death mechanism.

The episode clock includes artifact verification and all turns. At the maximum
eight episodes, a formal submission reserves at most 48 guest calls and 160
seconds of episode wall budget, plus bounded cleanup and application overhead.
There is no extra guest call for compilation, initialization, or final checking.
Under the daily reservation model of one unit per worst-case call of at most 15
seconds, this evaluator reserves **six units per selected episode**. These are
capacity reservations, not measured CPU or a billing amount.

This implementation keeps execution serial inside one worker. A shared CPU can
delay a correct program until its wall limit: a timeout is still a timeout.
Availability under competing workers must be measured separately from isolation
and correctness. The handoff retains calibration failures and the measured
concurrency boundary; it does not promise correct completion under arbitrary
host load. Operators must provision one serial worker per CPU performance budget
and avoid placing several judge workers on that same CPU. The current fixed CPU
affinity is preserved; starting additional containers does not create additional
CPU capacity. A production readiness or throughput replay must run separately
from deliberate same-CPU stress tests and unrelated worker preflights.

## Public execution and integration

`python_agent_tools_v1(problem, source, mode, sandbox, on_case)` is the formal
evaluator and requires both public and hidden episodes even for `mode="run"`.
It accepts only `run` and `submit`.

`project_public_problem(full_problem)` produces exactly
`{evaluator, language, function, assessment, tests}`, with public episodes only
and no reference or display metadata. `validate_public_problem(payload)`
enforces that exact allowlist and requires one to eight entirely public
episodes. `evaluate_public(payload, source, sandbox, on_case)` always performs
Run and has no mode parameter. A public package cannot authorize formal Submit.

The `on_case` callback runs before every guest turn so a worker can renew or
check its lease and cancel between calls. The current guest call remains bounded
by the profile. Only the reviewed `WasiSandbox` type is accepted. gVisor, host
Python, and substitute backends are rejected before launching a learner process.

Syntax, runtime, memory, output, and time failures use the existing result
vocabulary. Host protocol violations and unsuccessful reports are wrong answers.
Artifact failures, broken trusted transport, or missing runtime preflight are
platform unavailability and must not be presented as successful judging. A
source, test fixture, or fabricated trace cannot replace actual sandbox results.
