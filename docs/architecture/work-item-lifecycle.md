# Canonical Work-Item Lifecycle

## Status

**Milestone 2 architecture contract.** This document defines the canonical work-item stages already used by codex-web and the legal manual/API transition policy layered on top of them.

The roadmap names `created`, `ready`, `assigned`, `running`, `blocked`, `review`, and `completed` as illustrative lifecycle concepts. Codex-web already has a richer canonical model, so Milestone 2 strengthens that model rather than introducing a parallel set of states.

## Canonical stages

| Stage | Meaning |
| --- | --- |
| `implementation_active` | One implementation owner is actively responsible for the work. |
| `ready_for_validation` | Implementation has produced an artifact and a validation handoff is ready/pending. |
| `validation_running` | Validation owner has accepted the lane and validation is active. |
| `failed_with_action_owner` | Work is blocked/failed with one exact actionable owner and blocker. |
| `ready_to_close` | Validation is complete and the lane is ready for release/closure work. |
| `closed` | Terminal canonical lane for manual/API operations. |

`failed_with_action_owner` is intentionally resumable because the current state model does not yet persist a `previous_stage`; after resolving the blocker it may return to the appropriate non-terminal lane.

## Manual/API transition matrix

The policy in `codex_web/services/work_item_transitions.py` allows:

```text
implementation_active
  -> implementation_active
  -> ready_for_validation
  -> failed_with_action_owner

ready_for_validation
  -> ready_for_validation
  -> validation_running
  -> implementation_active
  -> failed_with_action_owner

validation_running
  -> validation_running
  -> ready_to_close
  -> implementation_active
  -> failed_with_action_owner

failed_with_action_owner
  -> any non-terminal canonical stage

ready_to_close
  -> ready_to_close
  -> closed
  -> implementation_active
  -> ready_for_validation
  -> validation_running
  -> failed_with_action_owner

closed
  -> closed
```

This prevents manual callers from skipping directly from implementation to validation-running/close states or silently resurrecting a closed lane.

## Authoritative transition mechanism

`WorkItemTransitionService` is the single stage-mutation primitive for existing work items. `WorkItemStateMachine` owns one instance and routes canonical stage changes through `_transition_work_item_stage`.

The following paths use that primitive:

- manual/API progress updates;
- handoff-driven stage changes;
- acknowledgement/rejection-driven stage changes;
- GitLab issue reconciliation;
- GitLab event reconciliation.

New work-item construction may set its initial stage directly because initialization is not a transition from an existing canonical stage. After creation, stage mutation belongs to the transition service.

`WorkItemService` no longer duplicates lifecycle validation at the API boundary. This keeps lifecycle policy and mutation inside the canonical state machine, including callers that invoke the state machine without passing through the HTTP service layer.

## External GitLab projection

GitLab reconciliation is deliberately distinct from the manual/API transition policy. An upstream GitLab issue can close or reopen and codex-web must reconcile that authoritative external event, subject to existing stale-event and handoff-preservation checks.

GitLab therefore calls the same `WorkItemTransitionService` mutation primitive with `external_projection=True`. This permits upstream close/reopen reconciliation without weakening the manual transition matrix or creating a second mutation implementation.

## Failure contract

An illegal manual transition fails closed with HTTP `409` and structured detail:

```json
{
  "code": "invalid_stage_transition",
  "from_stage": "implementation_active",
  "to_stage": "closed",
  "source": "progress_updated",
  "allowed_targets": [
    "failed_with_action_owner",
    "implementation_active",
    "ready_for_validation"
  ]
}
```

This is deterministic and requires no model reasoning.

## Tests

The transition-policy tests evaluate every source/target pair in the canonical stage matrix. Transition-service tests prove legal mutation, fail-closed illegal mutation, and the explicit external-projection exception. State-machine tests prove that direct canonical callers cannot bypass the transition policy by avoiding `WorkItemService`.
