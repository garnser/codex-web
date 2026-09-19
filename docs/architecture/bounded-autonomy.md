# Bounded autonomy controller

## Status

**Controlled-production-autonomy policy over the canonical event-driven control plane.** The autonomy controller consumes canonical event facts, separates deterministic observation/reasoning/execution, and applies one persisted effective autonomy policy before any autonomous external side effect.

## Cycle contract

Every autonomous cycle follows this order:

```text
Canonical event
    ↓
Deterministic observation
    ├── resolved → record cycle, zero reasoning
    └── unresolved
          ↓
      mode / trigger / threshold / depth / cooldown gates
          ↓
      effective autonomy level (observe / recommend / prepare / execute)
          ↓
      bounded reasoning retries
          ↓
      scoped token / cost / monetary / cloud / action / production-change budgets
          ↓
      provider contract + resource-risk resolution
          ↓
      production qualification / maintenance-window / rollback / verification gates
          ↓
      canonical ApprovalRequest when policy requires human quorum
          ↓
      required provider preflight
          ↓
      durable ActionIntent creation
          ↓
      existing authority + entitlement + security rechecks
          ↓
      worker execution / verification outside this controller
```

The controller never calls an ActionProvider directly. External effects are
represented as `ActionIntentCreate` records and cross the existing #137
ActionIntent boundary. A denied authority/security decision results in a
blocked autonomy cycle and no provider execution.

## Deterministic-first reasoning gate

A caller must provide an `AutonomyObservation`. If
`deterministic_resolved=true`, the cycle terminates without invoking a
reasoner regardless of its score.

For unresolved events, reasoning is permitted only when all configured gates
pass:

- autonomy mode is `active`;
- recursion depth is within the configured maximum;
- the event type is allowed by optional trigger rules;
- the deterministic reasoning score meets the threshold;
- the event-class/cycle cooldown is not active;
- neither dry-run nor simulation mode is suppressing execution.

No idle loop invokes the reasoner. Event-driven GitLab routing and legacy
watchdog fallbacks both pass through this boundary before an agent prompt is
sent.

## Effective autonomy policy

Autonomy is staged rather than binary:

- `observe` records deterministic observations only and never invokes reasoning;
- `recommend` may reason but never prepares or queues side effects;
- `prepare` may call the provider's declared prepare/preflight capability but never creates an executable ActionIntent;
- `execute_low_risk` permits only low-risk execution;
- `execute_bounded` permits low/medium risk execution within configured limits;
- `execute_broad` can permit high/critical execution, but production-class actions remain subject to configured qualification, approval, rollback, verification, maintenance-window and budget gates.

The effective policy is resolved from the workspace default plus deterministic project, canonical operational-role, and action-ID overrides. More-specific matching overrides apply after less-specific ones. Resource risk can only raise the effective action risk; a provider cannot label a critical production target as low risk to evade policy.

Budget charges for monetary impact, cloud spend and production-change count are policy-owned configuration. They are never accepted from model output. Model output may report token/cost usage, which is checked against the already-resolved policy budget before any ActionIntent is queued.

`GET /api/autonomy/policy/effective` provides a deterministic preview of the resolved level, limits and matched override IDs for an authenticated actor/project/action scope.

## Production qualification and approvals

For broad production autonomy, policy can require named canonical Evidence for evaluation, observability, release readiness, incident readiness, recovery, capacity, upgrades, audit integrity, worker-plane health and replicated ownership. Missing, failed, expired, stale or incorrectly scoped evidence fails closed.

High/critical actions can require canonical `ApprovalRequest` quorum. Critical actions default to two distinct human approvers. The exact normalized action request is hashed into the approval target, so approval for one target/version cannot be reused for a changed action. Approved requests are consumed before an executable ActionIntent becomes visible to workers.

Rollback/verification requirements are enforced against provider capabilities before queueing. A policy-required preflight executes the provider's prepare contract before ActionIntent creation. Production maintenance windows use explicit IANA timezones and deterministic wall-clock rules.

Break-glass is not a local bypass flag. When enabled, a human requests a canonical `ApprovalRequest` requiring distinct-human quorum and MFA, then activates a time-bounded grant tied to the exact autonomy-policy fingerprint. Activation consumes the approval and records the grant in autonomy state. A policy revision invalidates use of an old grant because its fingerprint no longer matches.

## Loop and blast-radius safeguards

Controls are persisted in the shared SQLite state store and include:

- pause, resume, and global kill state;
- dry-run and simulation modes;
- reasoning score threshold;
- repeated-event cooldown;
- maximum reasoning attempts with exponential backoff;
- maximum recursion depth;
- maximum actions created by one cycle;
- scoped model-token and model-cost limits;
- scoped monetary-impact and cloud-spend limits;
- scoped production-change counts;
- optional allowed trigger event types;
- project/role/action autonomy overrides;
- production maintenance windows and qualification gates.

Duplicate cycle keys for the same canonical event return the previously
recorded cycle. Canonical ingress also removes duplicate provider deliveries
before the autonomy controller sees them.

Reasoning failures exhaust a bounded retry count and enter a durable dead-letter
history. Recursion or action-budget violations fail closed and are also
dead-lettered.

## Observability, audit, and attribution

The canonical autonomy-cycle state remains the immediate control-plane record.
Every terminal cycle path is additionally appended to the tamper-evident autonomy
autonomy audit described in [autonomy-audit.md](autonomy-audit.md). Runtime logs
and dashboard counters remain telemetry and cannot substitute for that audit.

Each cycle records metadata only:

- canonical event ID/type/source;
- correlation and causation IDs;
- tenant/workspace scope;
- cycle key and recursion depth;
- reasoning score, whether reasoning ran, and attempt count;
- number of proposed actions and resulting ActionIntent IDs;
- effective autonomy level and policy fingerprint;
- canonical ApprovalRequest IDs and any break-glass grant ID;
- scoped budget usage for tokens/cost/monetary/cloud/production-change dimensions;
- outcome/reason/error and timestamps.

Recent cycle and dead-letter history is exposed through `GET /api/autonomy`.
Reconstructable audit history, integrity roots, reliability/efficiency metrics,
safety signals and periodic verification are exposed through
`/api/autonomy/audit`. Production `execute_broad` policy can require fresh
PASS Evidence for both audit integrity and measured reliability.
Control mutations require autonomy-admin authorization (and MFA for human
administrators):

- `PATCH /api/autonomy/control`
- `POST /api/autonomy/pause`
- `POST /api/autonomy/resume`
- `POST /api/autonomy/kill`
- `GET /api/autonomy/policy/effective`
- `POST /api/autonomy/break-glass/requests`
- `POST /api/autonomy/break-glass/activate`
- `GET /api/autonomy/break-glass`

## Legacy watchdog migration

Owner-work, release-gate, orchestrator, split-brain, and work-item-SLA fallback
reasoning now creates a canonical `work.transition` event and passes through
this controller. Purely deterministic watchdog repairs, such as expiring an
unacknowledged handoff and updating typed canonical state, remain model-free.

## UI follow-up

Issue #121 owns the unified Autonomy Control Center and explain-action UI. It should consume canonical autonomy policy/cycle, ApprovalRequest, ActionIntent, Evidence and readiness state rather than inventing browser-local safety or authorization truth.
