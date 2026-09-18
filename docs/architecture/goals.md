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

`completed` and `cancelled` are terminal in the M5 foundation. Completion verification is added by #106; this foundation does not invoke a model to decide lifecycle state.

Human mutations require tenant administrator authority plus MFA/step-up assurance. Service automation requires the explicit `goals:admin` scope. Reads remain tenant-scoped canonical projections.

## Machine-readable success criteria

A criterion can be:

- `manual` — a structured required statement awaiting later verification; or
- `metric` — a metric key plus `eq`, `gte`, or `lte` operator and a target value.

Metric criteria are deliberately provider-neutral. The Goal record defines the expected outcome; #106 owns bounded evaluation/completion verification and may attach canonical measurements/evidence without changing the criterion contract.

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

#106 will use these bindings when committing proposed decomposition so generated work remains attributable to the originating Goal.

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

Issue #106 persists decomposition as canonical proposal state **before** any model output may become Work Items. The first slice is deterministic and model-independent:

- a proposal is scoped to one exact Goal revision;
- it records the Goal's reasoning-budget snapshot plus explicit maximum planning depth and proposed-item count;
- every proposed item names an existing tenant-visible project and may declare a parent and blocking dependencies only within the proposal;
- item IDs are unique and parent/blocking graphs must be acyclic;
- structural hard limits cap proposal depth and item count even if a client requests more;
- accepted, rejected, and revised proposals are revisioned with actor/reason/timestamp events;
- acceptance fails when the Goal changed after the proposal was produced, forcing an explicit revision against current Goal state;
- revising a proposal refreshes the exact Goal revision and reasoning-budget snapshot before it can be reviewed again.

Creating or accepting a proposal does **not** create canonical Work Items and does not call a model. The later generation slice may use the Model Gateway to produce strict structured proposal JSON, but it must validate through this same deterministic contract and attribute every invocation to the Goal. The later commit slice must use the project TaskSource CREATE capability added by #310 and persist each external creation through the ActionIntent/ActionProvider side-effect boundary before provider execution.

This separation makes preview/review durable and auditable: models propose bounded structure, authorized operators accept/revise/reject, and side effects remain a later explicit phase.

## M5 handoff

This foundation intentionally does **not** perform Goal decomposition or LLM-driven completion judgment. Issue #106 builds on this contract to add:

- bounded Goal → proposed-work decomposition;
- accepted proposed work committed only through the project's authoritative TaskSource CREATE capability, with the external side effect persisted through the canonical ActionIntent/ActionProvider boundary before provider execution;
- review/accept/revise/reject before canonical work creation where policy requires;
- completion evaluation and evidence;
- Goal workspace/UI;
- end-to-end Goal → Decision/Project → Work → Result traceability.

Any reasoning added there must follow the Token Efficiency Ruleset: deterministic-first gating, minimum sufficient context, bounded calls/depth, structured output, and Goal-attributed token/cost accounting.
