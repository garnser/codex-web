# First-class Goals

Goals are canonical tenant-scoped outcome records. They provide the structured objective, success conditions, budgets, constraints, risk, approvals, and Work Graph traceability that later autonomous reasoning must consume rather than reconstructing from chat history.

The core rule is:

> **Goal state is application data. Models may propose plans for a Goal; they do not own Goal lifecycle, budgets, health, progress, or authority.**

## Canonical Goal state

A Goal records:

- stable Goal ID plus Organization/Workspace scope;
- title and description;
- accountable owner identity;
- lifecycle status and priority;
- optional target date;
- structured success criteria;
- constraints and risk metadata;
- goal-level reasoning/token/cost/retry/handoff budgets;
- approval requirements;
- one or more project/Work Graph bindings;
- current revision and creation/update provenance.

Mutations never rewrite revision history. Every accepted revision or lifecycle transition creates an immutable `GoalRevision` snapshot with actor, timestamp, and reason.

## Lifecycle

The first code-owned lifecycle is intentionally small:

```text
draft ──→ active ──→ completed
  │         │
  │         ├──→ paused ──→ active
  │         │       │
  └─────────┴───────┴──→ cancelled
```

`completed` and `cancelled` are terminal in the Goal lifecycle foundation. Completion verification is handled by the canonical Goal evaluation path; lifecycle state is not decided by an unconstrained model call.

Human mutations require tenant administrator authority plus MFA/step-up assurance. Service automation requires the explicit `goals:admin` scope. Reads remain tenant-scoped canonical projections.

## Machine-readable success criteria

A criterion can be:

- `manual` — a structured required statement awaiting later verification; or
- `metric` — a metric key plus `eq`, `gte`, or `lte` operator and a target value.

Metric criteria are deliberately provider-neutral. The Goal record defines the expected outcome; bounded Goal evaluation/completion verification may attach canonical measurements and Evidence without changing the criterion contract.

## Goal budgets

`GoalBudget` supplies deterministic upper bounds for autonomous reasoning:

- maximum input tokens;
- maximum output tokens;
- maximum model calls;
- maximum cost;
- maximum retries;
- maximum handoffs.

These are Goal-level limits in the token-efficiency hierarchy. They do not cause model calls by themselves and do not replace stricter project/work-item/role/model policy. A downstream execution must use the minimum applicable budget.

## Project and Work Graph traceability

A Goal may bind multiple projects. Each `GoalWorkGraphBinding` either:

- includes the entire canonical project Work Graph; or
- names one or more parent-root Work Item refs whose parent-descendant subgraphs belong to the Goal.

Binding validation is deterministic and tenant-scoped:

- the project must exist in the Goal tenant;
- each explicit root must exist in that project's canonical Work Graph;
- one Goal cannot bind the same project twice.

This lets one Goal produce work across multiple projects while keeping the originating outcome inspectable. The service also provides reverse lookup from a canonical Work Item ref to every visible Goal whose bound project/subgraph contains it. Reverse lookup is derived from the same Goal binding plus Work Graph edges; codex-web does not write a second mutable Goal-owner field into Work Item state merely for navigation.

Goal decomposition uses these bindings when committing proposed work so generated Work Items remain attributable to the originating Goal.

## Deterministic progress

Goal progress is derived from canonical Work Graph nodes, not agent prose. Over the unique set of bound Work Items the service records:

- project/work-item count;
- completed, failed, cancelled, active, runnable, and blocked counts;
- deterministic completion fraction.

When an explicit parent root is used, progress includes that root plus canonical downstream `parent` descendants. Overlapping bindings are deduplicated.

## Deterministic health

Health is a structured projection with explicit reasons:

- `blocked` when the Goal is paused or every non-terminal bound Work Item is blocked with nothing runnable;
- `at_risk` when bound work failed/cancelled, the target date passed before completion, or high/critical risk metadata exists;
- `on_track` when subordinate work exists without a deterministic blocking/risk signal, and for completed Goals;
- `unknown` when no subordinate work is bound yet.

This is intentionally code-owned. Later reasoning may explain or propose remediation, but it must not silently overwrite health.

## Revision and event provenance

Every create/revise/status transition records:

- current Goal revision;
- immutable full revision snapshot;
- actor identity;
- reason;
- timestamp;
- corresponding Goal event.

Historical scope, criteria, budget, and relationship changes can therefore be inspected without replaying chat history.

## Bounded decomposition proposal and review contract

Goal decomposition persists canonical proposal state **before** any model output may become Work Items. The proposal boundary is deterministic and model-independent:

- a proposal is scoped to one exact Goal revision;
- it records the Goal's reasoning-budget snapshot plus explicit maximum planning depth and proposed-item count;
- every proposed item names an existing tenant-visible project and may declare a parent and blocking dependencies only within the proposal;
- item IDs are unique and parent/blocking graphs must be acyclic;
- structural hard limits cap proposal depth and item count even if a client requests more;
- accepted, rejected, and revised proposals are revisioned with actor/reason/timestamp events;
- acceptance fails when the Goal changed after the proposal was produced, forcing an explicit revision against current Goal state;
- revising a proposal refreshes the exact Goal revision and reasoning-budget snapshot before it can be reviewed again.

Creating or accepting a proposal does **not** create canonical Work Items. Model-assisted generation may use the Model Gateway to produce strict structured proposal JSON, but the result validates through this same deterministic contract and every invocation remains attributed to the Goal.

Accepted proposals enter a separate durable commit lifecycle:

- all proposed projects and `task-source/authoritative` ActionProvider bindings are resolved and side-effect-free `prepare` checks pass before commit state is opened;
- parent and blocking relationships are project-local because canonical Work Graph edges cannot cross project boundaries;
- the proposal persists one immutable commit-plan record per proposed item before any task-creation intent is queued;
- each external task create is persisted as a Goal-attributed ActionIntent and remains queued for the leased ActionIntent worker rather than executing inline in the Goal request;
- per-item commit state exposes planned, queued, succeeded, failed, cancelled, uncertain, reconciliation-required, and rolled-back outcomes;
- reconciliation accepts only actual canonical Work Item refs returned by successful ActionResults and verifies those refs are visible in the expected project;
- only after every proposed item resolves successfully are parent/blocking edges materialized and generated root Work Items merged into the Goal's Work Graph bindings;
- the proposal reaches `committed` only after Work Graph and Goal traceability are durable.

Unknown non-idempotent TaskSource creates are never replaced or blindly retried. A commit may remain `committing` with an explicit blocker until ActionIntent reconciliation establishes the authoritative outcome. Re-running commit only queues records that never obtained an ActionIntent; it preserves existing intent identity for all attempted external work.

This separation makes preview/review/commit durable and auditable: models propose bounded structure, authorized operators accept/revise/reject, ActionIntent owns every external mutation, and canonical Goal traceability is finalized only from observed authoritative results.

## Deterministic completion evaluation

Goal completion is a verified state transition, not an agent assertion. A completion
evaluation is persisted against one exact Goal revision and records the actor,
reason, criterion observations, current bound Work Item outcomes, findings and
eligibility.

Success criteria are evaluated without model reasoning:

- manual criteria require an explicit verification observation;
- metric criteria require an explicit observed value and use the code-owned
  `eq`, `gte` or `lte` operator from the Goal definition;
- observations retain source/reference/timestamp provenance;
- missing required observations fail closed;
- unknown or duplicate criterion IDs are rejected rather than ignored.

Every Work Item in the Goal's canonical bound project/subgraph must currently have
terminal outcome `completed`. Active, failed, cancelled or missing bound work is
reported as an explicit blocker.

The `active -> completed` transition requires the caller to name the **latest**
passing completion evaluation for the current Goal revision. The service rechecks
bound Work Items immediately before completing so a previously passing snapshot
cannot conceal later Work Item drift. Revising the Goal makes previous completion
evaluations stale automatically because their `goal_revision` no longer matches.

The accepted evaluation ID and completion timestamp are stored in the completed
Goal revision, while all evaluation records remain tenant-scoped durable
provenance. This allows operators to reconstruct exactly why completion was
accepted without replaying chat or invoking a model.

## Runtime execution bindings and durable continuation

Canonical Goals and provider-native execution objectives are deliberately separate.
A provider-native objective is an execution aid; it is never lifecycle or completion
authority for the canonical Goal.

`GoalExecutionBinding` is the durable provider-neutral join between those
domains. A binding is tenant/workspace scoped and pins:

- the canonical Goal ID and revision;
- project and optional Work Graph roots/Work Item refs;
- provider, runtime, AgentSession, thread, and execution owner identities;
- provider-native objective identity when the runtime advertises that capability;
- requested/active/idle/blocked/failed/completed/cancelled/unknown execution status;
- cursor/checkpoint, last turn/execution, lease/heartbeat, retry and recovery state;
- actor/reason plus correlation/causation provenance and immutable binding events.

A runtime objective can therefore be inspected beside a Goal without becoming a
second Goal record. Runtimes without native-objective support still use the same
binding and durable canonical cursor/session continuation; unsupported native
operations fail visibly.

### Continuation ownership and stop gates

Continuation is lease-owned. Only one worker can hold an unexpired binding lease.
Every bounded turn re-enters canonical policy before provider execution. Production
dispatch requires:

- the canonical Goal to remain active and on the pinned revision;
- global autonomy to remain active;
- the persisted exclusive Goal/Project/Work Graph scope to match the binding;
- deterministic Goal/Work Graph state to remain runnable;
- Goal retry budget and the stricter service recovery bound not to be exhausted.

A current-revision passing completion evaluation terminates the binding as completed.
Provider prose or a provider-native "completed" flag never completes the canonical
Goal. Failed/cancelled bound work, deterministic Work Graph blocking, missing
completion verification after all work is terminal, Goal pause/cancel, policy kill,
and retry exhaustion stop continuation before a new provider turn is started.

Successful terminal turn events release the current lease and schedule at most one
next bounded turn. Failure events persist bounded retry/backoff. Interrupted turns
release without automatic redispatch.

### Restart and unknown provider outcomes

Startup recovery scans only tenant/workspace scopes that have persisted bindings.
Idle/failed/requested bindings and expired active leases with a known-terminal
canonical AgentSession may re-enter the normal dispatcher.

Lease expiry alone is **not** proof that the provider stopped running. If an expired
ACTIVE lease still maps to a canonical RUNNING AgentSession—or the provider outcome
cannot be established—the binding moves to explicit `unknown` state. UNKNOWN
is non-runnable and excluded from automatic lease claims/recovery.

An MFA-authorized operator must reconcile UNKNOWN explicitly as idle, failed,
blocked, or cancelled. Reconciliation clears stale lease/backoff state and records a
distinct immutable audit event. It cannot assert canonical Goal completion.

### Goal workspace controls

The Goal workspace projects runtime ownership, session/provider, cursor/checkpoint,
last activity, attempts, lease state, and stop reason. Authorized controls use the
same server-side policy boundaries:

- Pause interrupts active runtime work and persists an operator-paused blocked binding;
- Resume is accepted only for that explicit operator-paused state and re-enters all
  continuation gates;
- Cancel interrupts active runtime work and terminates the binding;
- Reconcile is the only operator path out of UNKNOWN;
- terminal sessions with a discoverable native objective may be explicitly attached
  or rebound to a Goal whose Work Graph scope contains that project;
- an unbound native objective may instead be promoted into a **draft** canonical Goal.

Discovery is read-only and never creates canonical application state by itself.

### Binding audit reconstruction

Binding creation/update provenance and immutable events retain actor, reason,
correlation and causation IDs. Lease claim/release, operator pause/resume/cancel,
reconciliation and terminal state changes are recorded so audit history can answer:

- who authorized the binding;
- exactly which Goal revision / project / Work Graph scope could run;
- which provider/runtime/session executed it;
- which request or runtime event caused continuation;
- why continuation stopped or entered UNKNOWN.

## Goal-domain handoff

The Goal domain provides bounded decomposition, review, durable ActionIntent-backed
commit, bidirectional Work Graph traceability, and deterministic completion
verification. The Goal workspace/UI then
projects these canonical contracts for operators without introducing client-side
shadow state.

Any reasoning added there must follow the Token Efficiency Ruleset: deterministic-first gating, minimum sufficient context, bounded calls/depth, structured output, and Goal-attributed token/cost accounting.
