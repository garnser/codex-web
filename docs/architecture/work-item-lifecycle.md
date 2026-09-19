# Canonical Work-Item Lifecycle

## Status

**Canonical work-item lifecycle contract.** This document defines the canonical work-item stages already used by codex-web, the legal manual/API transition policy, semantic terminal outcomes, and the structured execution lifecycle carried by each work item.

The canonical lifecycle is intentionally richer than a simple `created` → `ready` → `assigned` → `running` → `blocked` → `review` → `completed` sequence; integrations must strengthen and reuse this model rather than introducing parallel state machines.

## Canonical stages

| Stage | Meaning |
| --- | --- |
| `implementation_active` | One implementation owner is actively responsible for the work. |
| `ready_for_validation` | Implementation has produced an artifact and a validation handoff is ready/pending. |
| `validation_running` | Validation owner has accepted the lane and validation is active. |
| `failed_with_action_owner` | Work is blocked/failed with one exact actionable owner and blocker; this is resumable, not terminal failure. |
| `ready_to_close` | Validation is complete and the lane is ready for successful closure. |
| `closed` | Persisted terminal lifecycle lane. The semantic result is carried by `terminal_outcome`. |

`failed_with_action_owner` is intentionally resumable because the canonical stage describes ownership/lane state, while execution retry/failure metadata is persisted separately inside the same `WorkItemState`.

## Terminal outcomes

`WorkItemState.terminal_outcome` is typed as one of:

- `completed` — successful work that closes from `ready_to_close`;
- `failed` — terminal failure when a blocked `failed_with_action_owner` item is closed;
- `cancelled` — work intentionally stopped from another active lane.

This keeps terminal failure distinct from a recoverable blocker while preserving `closed` for persisted-state and API compatibility.

An external task source may close an item without exposing enough provider-neutral information to classify the reason. Such external closure leaves `terminal_outcome` unset rather than guessing. A task-source adapter may classify the provider-specific close reason only when its contract exposes that information.

If an authoritative external source reopens a closed work item, the stale terminal outcome is cleared as the item returns to an active lifecycle.

## Execution lifecycle metadata

`WorkItemState.execution` is the canonical structured execution record for retry, deadline, failure, checkpoint, and model-usage state. It is embedded in the work item rather than persisted as a parallel task record.

The lifecycle contains:

- retry attempt, maximum-attempt policy, backoff, and last-retry timestamp;
- optional timeout and absolute deadline;
- structured failure category/code/message/retryability and recording time;
- latest compact checkpoint plus a bounded checkpoint history;
- cumulative model-call, token, estimated-cost, goal, and decision attribution.

All fields have safe defaults. Loading an older persisted `WorkItemState` with no `execution` member therefore produces the default lifecycle in memory and preserves the old item without a destructive migration.

Failure categories and codes are validated as non-empty data rather than a closed enum. Engines can add deterministic classifications without changing the persisted schema, while consumers still avoid inferring failure semantics from prose.

## Checkpoints and resume

A checkpoint is intentionally compact. It records the objective, current state, important decisions, blockers, changed files, next actions, and an audit actor/source/reason. Only the latest checkpoint is required for dispatch/resume; bounded history remains available for inspection.

The execution-contract builder copies only the latest checkpoint id and summary into the dispatch contract. Long-running work can therefore resume from canonical state without replaying the full event or conversation history.

Checkpoint history is capped at 20 entries per work item. The append-only work-item event log remains the audit trail and is queryable through the work-item history API.

## Audit and accounting

Execution lifecycle changes append attributed `WorkItemEvent` entries with top-level `actor`, `source`, and `reason` metadata. Existing events remain valid because these audit fields are optional.

Usage records are additive and accumulate calls, input/output/reasoning tokens, and estimated cost on the canonical work item. Optional `goal_id` and `decision_id` fields are attribution hooks for canonical Goal/Decision domains; they do not create those entities inside the work-item lifecycle.

The API surfaces:

```text
GET   /api/work-items/{ref}/history
GET   /api/work-items/{ref}/execution
PATCH /api/work-items/{ref}/execution
POST  /api/work-items/{ref}/checkpoints
POST  /api/work-items/{ref}/usage
```

Static-suffix routes are registered before the catch-all work-item route so provider refs containing slashes remain addressable.

## Manual/API transition matrix

The policy in `codex_web/services/work_item_transitions.py` allows:

```text
implementation_active
  -> implementation_active
  -> ready_for_validation
  -> failed_with_action_owner
  -> closed (cancelled)

ready_for_validation
  -> ready_for_validation
  -> validation_running
  -> implementation_active
  -> failed_with_action_owner
  -> closed (cancelled)

validation_running
  -> validation_running
  -> ready_to_close
  -> implementation_active
  -> failed_with_action_owner
  -> closed (cancelled)

failed_with_action_owner
  -> any active canonical stage
  -> closed (failed)

ready_to_close
  -> ready_to_close
  -> closed (completed)
  -> implementation_active
  -> ready_for_validation
  -> validation_running
  -> failed_with_action_owner

closed
  -> closed
```

Manual callers still cannot skip implementation directly into validation-running or ready-to-close, and cannot resurrect a closed lane. Closure is permitted from active lanes because completion, cancellation, and terminal failure are distinct deterministic outcomes.

## Authoritative transition mechanism

`WorkItemTransitionService` is the single stage-mutation primitive for existing work items. `WorkItemStateMachine` owns one instance and routes canonical stage changes through `_transition_work_item_stage`.

The following paths use that primitive:

- manual/API progress updates;
- handoff-driven stage changes;
- acknowledgement/rejection-driven stage changes;
- authoritative external issue reconciliation;
- authoritative external event reconciliation.

New work-item construction may set its initial stage directly because initialization is not a transition from an existing canonical stage. After creation, stage mutation belongs to the transition service.

`WorkItemExecutionLifecycleService` does not mutate canonical stage or ownership. It only updates the `execution` member of the same canonical `WorkItemState`, persists through the state machine's existing save seam, and appends attributed execution events.

## External task-source projection

External authoritative task-source reconciliation is deliberately distinct from the manual/API transition policy. An upstream item can close or reopen and codex-web must reconcile that authoritative event, subject to stale-event and handoff-preservation checks.

Provider adapters normalize into the provider-neutral TaskSource contract and use the same `WorkItemTransitionService` mutation primitive with `external_projection=True`. This permits upstream close/reopen reconciliation without weakening the manual transition matrix or creating a second mutation implementation.

## Failure contract

An illegal manual transition fails closed with HTTP `409` and structured detail. For example, implementation cannot jump directly to `validation_running`:

```json
{
  "code": "invalid_stage_transition",
  "from_stage": "implementation_active",
  "to_stage": "validation_running",
  "source": "progress_updated",
  "allowed_targets": [
    "closed",
    "failed_with_action_owner",
    "implementation_active",
    "ready_for_validation"
  ]
}
```

Execution lifecycle validation also fails deterministically. A retry attempt above `max_attempts`, lowering a retry policy below the current attempt, conflicting failure-clear/update requests, or an incomplete failure classification returns a structured `409`/`422` instead of relying on model interpretation.

## Tests

The transition-policy tests evaluate every source/target pair in the canonical stage matrix. Transition-service tests cover completed, cancelled, failed, unclassified external closure, and external reopen behavior. Execution-lifecycle tests cover safe migration defaults, retry-policy enforcement, structured failure/deadline state, compact checkpoints, attributed history, and additive usage accounting.
