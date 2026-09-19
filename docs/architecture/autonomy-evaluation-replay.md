# Autonomous evaluation and deterministic replay

## Status

**Canonical autonomy evaluation and replay contract.** Evaluation is an offline qualification
boundary for event-driven autonomy. It is deliberately separate from live
provider execution.

## Core rule

An evaluation run must be reproducible without mutating a real provider,
resource, repository, deployment, customer system, or credential backend.

The built-in `recorded` replay backend only returns an immutable recorded/mock
trace stored in the versioned scenario fixture. Additional replay backends may
be registered in code, but they must obey the same no-live-side-effect
contract.

## Versioned scenarios

An `EvaluationScenario` is tenant/workspace scoped and immutable at
`scenario_id + version`. It records:

- a fingerprinted canonical starting-state snapshot plus canonical object refs;
- a sanitized canonical event sequence;
- exact historical Definition Registry references, including record ID,
  revision, schema version and checksum;
- AgentProvider/runtime identity, runtime type and capability revision where
  applicable;
- model/provider version and prompt-template version/checksum/policy
  fingerprint where applicable;
- bounded retrieved-context references rather than full histories;
- deterministic expected invariants, budgets and regression thresholds;
- recorded historical, candidate and failure-injection replay fixtures;
- named suites and CI tier (`smoke`, `relevant`, or `release`).

Scenario event payloads reject obvious secret-bearing keys. Fixtures are not a
credential transport and must contain sanitized data only.

## Historical and candidate replay

Historical replay uses the exact pinned definition set captured by the
scenario. It never resolves "whatever is active now."

Candidate replay replaces explicit historical definition slots with exact
candidate `DefinitionReference` values. References are checked against the
immutable Definition Registry record and tenant/project scope before execution.
A draft/validated candidate can therefore be evaluated before publication.

A recorded trace must report the exact definition set it actually used.
Definition mismatches are deterministic assertion failures.

## Provider-neutral runtime attribution

Evaluation traces use the same provider-neutral concepts as the AgentRuntime
telemetry/conformance boundary:

- AgentProvider ID;
- runtime ID/type;
- capability revision and runtime version;
- observed model/provider IDs and versions;
- prompt-template version/checksum and model-policy fingerprint;
- token, cost, duration, tool/action and terminal-outcome facts.

The evaluator does not assume Codex. Runtime-specific replay belongs behind the
replay-backend contract.

## Deterministic assertions first

The evaluator checks normal code before any semantic evaluator is considered:

- terminal outcome;
- required state transitions;
- selected roles;
- exact definition set;
- action count;
- allowed/forbidden action kinds;
- required Evidence;
- retry limits;
- latency bounds;
- model-call/input/output token bounds;
- cost bounds;
- required no-LLM paths;
- policy-violation and intervention limits.

This first slice intentionally does not add an LLM-as-judge. A future semantic
quality evaluator must be bounded and may only supplement these deterministic
checks.

## Regression comparison

Candidate runs can reference a historical baseline run. Comparison records
capture:

- pass/fail change;
- token and cost deltas;
- latency delta;
- action-count delta;
- human-intervention delta;
- policy-violation delta;
- exact definition-resolution change.

Scenario thresholds determine whether increases are regressions. This makes
token/cost/action/retry regressions machine-detectable rather than review prose.

## Failure injection

Failure behavior is replayed from explicit recorded fixtures. Supported
injection identities include provider timeout, model outage, stale/duplicate
event, unknown action result, revoked secret, expired lease,
missing/incompatible definition and dependency failure.

The scenario must declare an injection and include a matching replay fixture;
the evaluator never simulates a failure by calling a live provider.

## Evidence and autonomy qualification

Each run emits canonical `policy_evaluation` Evidence when the
Artifact/Evidence service is configured. Evidence metadata contains the run,
scenario, fixture, replay backend, checksum and assertion outcome. Named suite
runs aggregate run/comparison IDs and Evidence IDs with one deterministic
qualification result.

production autonomy policy can therefore require a named passing suite without
re-running an LLM to decide whether qualification exists.

## API

- `GET/POST /api/evaluations/scenarios`
- `GET /api/evaluations/scenarios/{scenario_id}/{version}`
- `GET/POST /api/evaluations/runs`
- `GET /api/evaluations/runs/{run_id}`
- `GET /api/evaluations/comparisons`
- `POST /api/evaluations/suites/{suite_id}/run`
- `GET /api/evaluations/suites/runs`

Creating scenarios and executing replay/suite runs require canonical admin/MFA
(or the equivalent service scope). Reads remain tenant scoped.

## UI impact

The orchestration inspector should project these canonical records into the orchestration
inspector: scenario/fixture version, exact historical/candidate definitions,
runtime/model/prompt pins, deterministic failures, regression deltas, injected
failure and Evidence. Refreshing the inspector must remain read-only and must
not launch an evaluation or model call.
