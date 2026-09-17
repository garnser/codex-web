# Canonical Work-Item Lifecycle

## Status

**Milestone 2 architecture contract.** This document defines the canonical work-item stages already used by codex-web, the legal manual/API transition policy, and semantic terminal outcomes.

The roadmap names `created`, `ready`, `assigned`, `running`, `blocked`, `review`, and `completed` as illustrative lifecycle concepts. Codex-web already has a richer canonical model, so Milestone 2 strengthens that model rather than introducing a parallel set of states.

## Canonical stages

| Stage | Meaning |
| --- | --- |
| `implementation_active` | One implementation owner is actively responsible for the work. |
| `ready_for_validation` | Implementation has produced an artifact and a validation handoff is ready/pending. |
| `validation_running` | Validation owner has accepted the lane and validation is active. |
| `failed_with_action_owner` | Work is blocked/failed with one exact actionable owner and blocker; this is resumable, not terminal failure. |
| `ready_to_close` | Validation is complete and the lane is ready for successful closure. |
| `closed` | Persisted terminal lifecycle lane. The semantic result is carried by `terminal_outcome`. |

`failed_with_action_owner` is intentionally resumable because the current state model does not yet persist a `previous_stage`; after resolving the blocker it may return to the appropriate non-terminal lane.

## Terminal outcomes

`WorkItemState.terminal_outcome` is typed as one of:

- `completed` — successful work that closes from `ready_to_close`;
- `failed` — terminal failure when a blocked `failed_with_action_owner` item is closed;
- `cancelled` — work intentionally stopped from another active lane.

This keeps terminal failure distinct from a recoverable blocker while preserving `closed` for persisted-state and API compatibility.

An external task source may close an item without exposing enough provider-neutral information to classify the reason. Such external closure leaves `terminal_outcome` unset rather than guessing. A future task-source adapter may classify the provider-specific close reason when its contract exposes that information.

If an authoritative external source reopens a closed work item, the stale terminal outcome is cleared as the item returns to an active lifecycle.

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

Manual callers still cannot skip implementation directly into validation-running or ready-to-close, and cannot resurrect a closed lane. Closure is permitted from active lanes because completion, cancellation, and terminal failure are now distinct deterministic outcomes.

## Authoritative transition mechanism

`WorkItemTransitionService` is the single stage-mutation primitive for existing work items. `WorkItemStateMachine` owns one instance and routes canonical stage changes through `_transition_work_item_stage`.

The following paths use that primitive:

- manual/API progress updates;
- handoff-driven stage changes;
- acknowledgement/rejection-driven stage changes;
- authoritative external issue reconciliation;
- authoritative external event reconciliation.

New work-item construction may set its initial stage directly because initialization is not a transition from an existing canonical stage. After creation, stage mutation belongs to the transition service.

`WorkItemService` does not duplicate lifecycle validation at the API boundary. This keeps lifecycle policy and mutation inside the canonical state machine, including callers that invoke the state machine without passing through the HTTP service layer.

## External task-source projection

External authoritative task-source reconciliation is deliberately distinct from the manual/API transition policy. An upstream item can close or reopen and codex-web must reconcile that authoritative event, subject to stale-event and handoff-preservation checks.

The current GitLab integration calls the same `WorkItemTransitionService` mutation primitive with `external_projection=True`. This permits upstream close/reopen reconciliation without weakening the manual transition matrix or creating a second mutation implementation. Milestone 2 will move this projection behind the provider-neutral task-source contract.

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

This is deterministic and requires no model reasoning.

## Tests

The transition-policy tests evaluate every source/target pair in the canonical stage matrix. Transition-service tests cover completed, cancelled, failed, unclassified external closure, and external reopen behavior. State-machine tests prove terminal outcomes persist through canonical progress updates and that direct callers cannot bypass transition policy by avoiding `WorkItemService`.
