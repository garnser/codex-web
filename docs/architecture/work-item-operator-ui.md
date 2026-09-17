# Work-item operator surface

## Status

**Milestone 2 operator contract.** The work-item operator is a projection over canonical `Project`, `WorkItemState`, `TaskSource`, execution-contract, checkpoint, and event-history data. It does not create a parallel task database or UI-only source binding.

## Canonical data paths

The operator reads and mutates the following existing authorities:

- project source selection: `Project.authoritative_task_source` through `PUT/DELETE /api/projects/{project_id}/task-source`;
- work-item state: `WorkItemState` through `/api/work-items` and the canonical state machine;
- execution lifecycle: `WorkItemState.execution` and `/api/work-items/{ref}/execution`;
- event history: append-only `WorkItemEvent` history;
- execution contract: the validated contract produced by the installed work-item contract service;
- source reconciliation: registered `TaskSource` adapters and their declared capabilities.

The UI stores only ephemeral selection state such as the currently selected project/work item.

## Operator projection

`GET /api/work-items/{ref}/operator` returns one bounded explainability view containing:

- canonical stage, owner, handoff, blockers, routing diagnostics, artifact state, and execution lifecycle;
- authoritative source configuration and immutable external identity/provenance (`source_type`, instance, external id, revision, event cursor);
- live adapter availability and declared capabilities;
- effective project sandbox/approval policy plus the validated execution contract, role, agent binding, success criteria, and failure conditions;
- latest checkpoint and cumulative model/token/cost attribution;
- bounded chronological event history with actor/source/reason;
- reconciliation/execution diagnostics derived from canonical routing findings, structured failures, and diagnostic events;
- deterministic action eligibility for retry and reconciliation.

Canonical and external state are rendered in visually separate cards so projected provider facts cannot be confused with codex-web lifecycle state.

## Source configuration and resync

A project has exactly one optional `authoritative_task_source`; the Pydantic project model makes simultaneous mutable sources unrepresentable. The operator edits that same field and never keeps a second binding.

`POST /api/work-items/sync/{project_id}` resolves the configured source through the same `TaskSourceRegistry` used by normal work-item operations. A synthetic identity carrying the configured source type/instance is used only for adapter resolution; it is not persisted as a work item. The adapter must declare `discovery` before resync is allowed.

Each discovered normalized snapshot is passed through `TaskSourceWorkItemProjector`, preserving provider-neutral reconciliation and canonical transition rules.

## Reconcile and retry

`POST /api/work-items/{ref}/reconcile` requires a resolvable authoritative adapter and its `read` capability. It re-reads the external task and projects the normalized snapshot through the shared projector. An attributed `operator_reconciled` event records the explicit action.

`POST /api/work-items/{ref}/retry` is allowed only while the item is non-terminal, has an actionable owner, and has retry budget remaining. It increments the structured retry attempt through `WorkItemExecutionLifecycleService`, clears the previous failure classification, and enters the existing actionable-owner dispatch seam. It does not directly execute an agent or bypass dispatch policy.

These capability/lifecycle gates are deterministic. Later authority/RBAC milestones can add actor permission checks around the same canonical actions without changing their state model.

## UI implementation

`static/work_items_ui.js` creates a modal operator surface from the existing application shell. `codex_web.api.ui` loads the versioned module in the served index without modifying the legacy application bundle. The module calls only canonical APIs and uses no local persistence.

The UI supports:

- project selection;
- authoritative source type/instance/scope configuration;
- source capability and sync-health inspection;
- project resync;
- work-item list/detail inspection;
- canonical/external state comparison;
- retry and reconcile actions when the backend declares them allowed;
- execution contract, policy, checkpoint, usage, diagnostics, and event history inspection.

## Validation

Focused Python tests cover operator projection, retry bounds/dispatch, authoritative reconciliation, and project-source discovery using the provider-neutral reference adapter. Chromium tests cover the state distinction, provenance/contract/checkpoint/usage/history rendering, retry dispatch request, and canonical source-configuration write path.
