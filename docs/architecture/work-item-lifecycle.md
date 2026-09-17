# Canonical Work-Item Lifecycle

## Status

**Milestone 2 architecture contract.** This document defines the canonical work-item stages already used by codex-web and the legal manual/API transition policy layered on top of them.

The roadmap names `created`, `ready`, `assigned`, `running`, `blocked`, `review`, and `completed` as illustrative lifecycle concepts. Codex-web already has a richer canonical model, so Milestone 2 must strengthen that model rather than introduce a parallel set of states.

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

## External GitLab projection

GitLab reconciliation is deliberately separate from the manual/API transition policy. An upstream GitLab issue can close or reopen and codex-web must reconcile that authoritative external event, subject to existing stale-event and handoff-preservation checks.

Therefore a GitLab projection may cause a state change that a manual progress call would not be allowed to request. This is an explicit source distinction, not a bypass hidden in the transition table.

## Enforcement boundary

The first transition-policy increment validates explicit stage requests at `WorkItemService`, before the canonical mutation method is invoked. This covers the normal handoff, acknowledgement, and progress API path while retaining compatibility with lightweight test/extension doubles that do not expose state lookup.

This increment does **not** yet claim that all state mutation is centralized. GitLab projection and several internal state-machine paths still assign canonical stage as part of reconciliation. A later Milestone 2 slice must route internal manual mutations through one authoritative transition mechanism and remove scattered direct assignments where they are not external projections.

## Failure contract

An illegal manual transition fails closed with HTTP `409` and structured detail:

```json
{
  "code": "invalid_stage_transition",
  "from_stage": "implementation_active",
  "to_stage": "closed",
  "source": "work-item-progress",
  "allowed_targets": [
    "failed_with_action_owner",
    "implementation_active",
    "ready_for_validation"
  ]
}
```

This is deterministic and requires no model reasoning.

## Tests

The transition-policy tests evaluate every source/target pair in the canonical stage matrix. Service-level tests additionally prove that an explicit illegal transition is rejected before mutation, while historical lightweight service doubles without canonical state lookup continue to work.
