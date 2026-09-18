# Dependency-aware Work Graphs

This document defines the canonical M4 work-graph domain for codex-web.

The graph is deterministic application state and logic. It does **not** require an
LLM to decide whether work is runnable, blocked, structurally invalid, or affected
by a failed dependency.

## Sources of truth

Keep these responsibilities distinct:

- **Work Item lifecycle state** remains canonical for each node's current stage,
  terminal outcome, explicit blocker, and blocking findings.
- **Work Graph state** stores only versioned graph relationships and their
  dependency-failure behavior.
- **Readiness, runnable work, progress, and critical path** are derived on read.
  They are not persisted as a second mutable truth.
- **Execution workspace/lease state** remains the M3 execution-isolation
  boundary. A runnable graph node is not permission to bypass worker/workspace,
  authority, policy, or ActionIntent checks.

When a blocking dependency completes successfully, downstream readiness changes
automatically because the next evaluation reads current Work Item state.

## Relationship semantics

Edges are directed and tenant/project scoped.

### Parent

`parent -> child`

Parent relationships describe hierarchy/decomposition. A child may have at most
one parent. Parent relationships are acyclic.

Parent relationships do not themselves block execution and do not carry a
dependency-failure policy.

### Blocks

`blocker -> blocked`

A `blocks` edge means the target cannot become graph-runnable until the source
has completed successfully.

Blocking relationships are acyclic. Fan-in and fan-out are allowed:

- fan-in requires all upstream blockers to complete successfully;
- fan-out allows multiple downstream nodes to become runnable in parallel once
  their shared blocker clears.

A dependency that is still active produces a deterministic blocking explanation.
A dependency that ends `failed` or `cancelled` also emits a
`WorkDependencyImpact` with one explicit downstream behavior:

- `pause`
- `fail`
- `replan`
- `escalate`

The graph engine reports that impact; later orchestration owns applying the
corresponding lifecycle/policy action. The graph engine does not silently mutate
downstream Work Items merely because it detected an impact.

## Readiness

A non-terminal Work Item is graph-runnable only when:

1. it has no canonical `blocker`;
2. it has no canonical `blocking_findings`;
3. every incoming `blocks` dependency has completed successfully.

Terminal Work Items are reported as terminal rather than runnable/blocked.

Missing dependency nodes fail closed and keep the downstream node blocked.

The deterministic explanation includes:

- blocking Work Item refs;
- canonical blocker/finding facts;
- dependency lifecycle facts;
- failed/cancelled dependency behavior.

## Cycle prevention

Cycles are rejected before persistence independently for:

- parent hierarchy;
- blocking dependencies.

Stored graph corruption is also detected by deterministic algorithms such as
critical-path calculation rather than being accepted as runnable state.

## Traversal and project snapshots

The graph service provides deterministic upstream/downstream traversal and a
project snapshot containing:

- all tenant-visible project nodes;
- graph edges;
- per-node readiness;
- currently runnable refs;
- dependency-failure impacts;
- graph progress;
- critical path.

Traversal order and tie-breaking are stable so tests, APIs, replay, and UI can
produce the same explanation.

## Critical path

Until duration estimates become canonical data, the critical path is the longest
`blocks` chain measured in nodes/edges. Equal-length paths use lexical
tie-breaking.

The implementation uses iterative topological dynamic programming rather than
recursive traversal so deep valid graphs do not depend on Python recursion
limits.

A later scheduling/estimation feature may introduce weighted critical paths only
when the weights themselves have a canonical source and versioned semantics.

## Progress

Project graph progress reports separate counts for:

- total;
- completed;
- failed;
- cancelled;
- active;
- runnable;
- blocked.

`completion_fraction` is successful completions divided by total nodes. Failure
and cancellation are deliberately not counted as successful completion.

## Persistence and audit

Graph state is stored in the shared SQLite document store using the versioned
`work-graph-state` contract.

Edge additions/removals append graph audit events with:

- actor;
- edge identity;
- relationship;
- scope;
- timestamp.

APIs/UI added by #104 must mutate this service rather than maintaining
browser-local or provider-local dependency state.

## Scope and security

Edges may only connect Work Items in the same Organization, Workspace, and
Project. Cross-tenant object existence must fail closed.

Graph readiness is an execution prerequisite, not execution authority. Later
orchestration must still honor:

- role/identity authority;
- policy/approval;
- entitlements where applicable;
- Resource Catalog scope;
- worker capability and fenced leases;
- isolated execution workspace;
- ActionIntent/provider authority for external side effects.

## UI/API follow-up

Issue #104 owns the canonical graph APIs and interactive graph UI. It should
consume this service for:

- dependency creation/removal;
- cycle/conflict validation;
- `what can run now?`;
- `why blocked?`;
- critical-path and parallel-work highlighting;
- progress and failure-impact display;
- deep links to canonical Work Item detail.

No UI-owned graph state is permitted.
