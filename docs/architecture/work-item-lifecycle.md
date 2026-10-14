# Canonical Work-Item Lifecycle

The canonical work-item audit window uses `work_item_events.records.v1` with
versioned metadata and transactionally assigned sequence keys. Each append writes
one event, advances the sequence and removes at most one expired row, retaining
the existing latest-10,000-event window without rewriting unrelated history.
Concurrent replicas share the namespace transaction lock. First access migrates
the old `work_item_events` list once. Retention changes require an explicit
migration. The JSONL forensic mirror and its existing history/checkpoint API
semantics remain available.
Forensic history reads filter the requested work-item ref before validating an
event model. They still scan JSONL to preserve the complete historical watermark;
unrelated events must not incur model validation cost.

The old StateStore list is a rollback checkpoint, refreshed on controlled
shutdown; it is not a second active audit authority. Stop older writers before
upgrading. After a rollback has produced new legacy writes, reconcile that
checkpoint before rebuilding/re-entering keyed storage. Older and keyed audit
writers must not run concurrently. The JSONL mirror is still appended after the
canonical commit, with the same failure ordering as the historical implementation.

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

Automatic owner dispatch for `failed_with_action_owner` selects the canonical
`next_owner`, falling back to `current_owner` when no next action owner is set.
Other stages retain current-owner precedence. Binding selection, queued dispatch,
post-recovery revalidation and prompt addressees use the same rule. An accepted
handoff is described as live only when its recipient matches the actionable owner. This does not change the
implementation/validation owners or accepted handoffs, and pending handoffs
remain ineligible for automatic owner dispatch. Issue and merge-request refs
remain distinct; selecting an owner does not grant provider writeback capability.

Structured handoff dispatch carries the canonical work-item ref and repository
scope through the same scoped turn boundary as actionable-owner dispatch. The
recipient's published execution profile still controls repository access; a
scratch-only coordinator receives task identity without repository grants.
Queue admission does not acknowledge the handoff on the recipient's behalf.

Archiving intake preserves its original `acknowledged_at`: unacknowledged
pending intake remains unset, and accepted/rejected intake keeps the actual
recipient receipt time. Expiry, supersession and closure use their attributed
audit event times for resolution; they never synthesize a recipient ACK or
overwrite an earlier one. Historical records are retained without guessing
whether a legacy archive timestamp represented an actual receipt.

A recognized pending handoff recipient retains its own execution role even
when ownership or status drift requires reconciliation. Those findings remain
in the contract and the separate Orchestrator reconciliation path; they do not
turn a James, Quinn or Carl intake into an Orchestrator-only prompt. Unknown
recipients and items without a recognized pending intake retain the
Orchestrator fallback for drift. This does not grant repository access or
acknowledge intake.

The SLA expiry cycle mutates only the current matching pending handoff under
the canonical per-record lock. It never saves its detached collection snapshot
after awaited dispatch. Concurrent replacement, acknowledgement, closure and
unrelated progress survive; an expiry event is emitted only after the matching
current intake is actually archived.

After normal broker identity, tenant, Project, resource, execution-profile and
operation authorization, the scoped work-item read includes a bounded
`owner_dispatch` projection. It uses the same actionable-owner and responsible
binding resolver as owner steering. Only an actionable, open item with no
pending handoff and a same-Project owner binding exposes `expected_owner` and
`expected_thread_id`; every other result redacts both values and returns a
stable `blocked_reason`. The projection is read-only and does not expose thread
lists, queues, messages or authority. It is a preflight observation, not a
reservation: steering reloads canonical state under its per-item lock and
revalidates the owner, lifecycle conditions, Project and exact thread before
dispatch.

An identical normalized progress update remains an attributed audit receipt and
owner activity acknowledgement. It does not advance `last_meaningful_update_at`
or schedule another owner dispatch/continuity check solely for the unchanged
receipt. The attributed `progress_updated` audit payload includes
`meaningful_change` so operators can distinguish a receipt from new evidence.
Comparison uses canonical state excluding receipt/activity/provider
observation clocks; notes, findings, action/owner, handoff, artifact and source
revision changes remain meaningful. Provider synchronization and public progress
acknowledgements still run. Independent watchdog retry/cooldown behavior remains
unchanged; this is not permanent blocked-work suppression or inferred dependency
readiness.

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

A source refresh that repeats the existing blocked stage and owner preserves the explicit next action owner, pending handoff, and recorded blocker. A repeated blocked label is neither a new routing decision nor recipient acknowledgement. Newly blocked transitions and changed source owners still reconcile and supersede obsolete pending handoffs; closure and reopening retain their existing rules.

Canonical progress also preserves a pending handoff when the stage, current owner, and recipient are unchanged. Findings, artifact metadata, notes, or a repeated blocked status update the same lane without resolving intake; its derived status remains awaiting confirmation. To deliberately supersede pending intake through a nonterminal progress update, read the current handoff and supply its exact `requested_at` as `expected_handoff_requested_at`. Missing or mismatched preconditions return `409 pending_handoff_conflict` before state persistence, successful progress audit, provider writeback or dispatch. A supplied precondition also fails if that intake was replaced, accepted, expired or removed, even for an otherwise harmless update. Matching progress supersession still obeys stage/routing rules and never creates a recipient ACK. Recipient ACK and new structured handoff are the normal delivery operations; authoritative closure and verified ActionIntent advancement retain their existing semantics. This guard identifies the pending intake, not a revision lock on the entire Work Item.

Implementation attribution is distinct from the actor performing a repair and from the owner of the current lifecycle lane. Operational coordination identities such as `operator` and `orchestrator`, plus the configured validation and release owners, are never inferred as `implementation_owner` from progress or handoff state. Existing attribution survives operator coordination and handoff expiry. An explicit operator reconciliation may correct it only from fresh, exact-resource provider assignment evidence and records the correction without claiming validation, closure, or external-source ownership.

An issue already at `ready_for_validation` may retain that stage, its
implementation owner, and its next validation owner when canonical state holds
a fresh provider-verified open merge-request relation. Ordinary issue refreshes
that did not query relations preserve the proof; fresh evidence that the
relation is absent or no longer open returns the issue to implementation. The
relation does not prove CI, approval, merge, acceptance, or closure.

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

## Artifact event repository associations

GitLab MR and pipeline projections resolve the Project tenant and exact GitLab
repository alias through the same canonical dependencies as issue projections.
Creation and update persist these associations. A stale event or an accepted
handoff may repair routing metadata without changing the protected lifecycle,
owner, or artifact fields. Missing aliases remain unresolved and visible in
Project readiness; projections never substitute unrelated Project Resources.
Artifact projection uses keyed state reads and writes when available so one MR
event does not scan or rewrite the complete work-item collection.

## Queued task-source notification recency

Unaddressed GitLab reconciliation notifications for the same thread and Work Item coalesce by canonical queue admission `created_at`, with queued ID as the deterministic equal-time tie-break. List position is not recency: retry-front insertion and restoration can reorder entries. Other threads/references, explicit handoffs, user messages and notifications carrying reply targets retain their delivery semantics. Terminal cleanup removes only unaddressed GitLab reconciliation signals. Startup compaction records removed/retained notification IDs and Work Item/Project references through existing bot audit events after queue persistence succeeds; message bodies and secrets are excluded. Notification compaction is neither recipient acknowledgement nor issue completion.

### Pending CI and validation ownership

A running, pending, created or queued check on the exact MR head is an asynchronous prerequisite. The validation owner records the MR, head, pipeline/check identities and observed statuses in a canonical checkpoint, retains ownership in the validation lane, and stops until terminal provider evidence. Pending CI is not rejection or failed work and does not justify an implementation or Orchestrator handback. Model polling and unchanged CI retries are prohibited. The primary validation disposition requirement accepts an exact-head pending-CI checkpoint alongside merge/close and evidenced rejection or defect handback; it must not separately require a handback to finish the turn. A terminal failure identifies one capable repair owner: code defects go to the implementation owner, while runner, dependency and environment defects go to Carl; successful checks still require independent artifact acceptance and all merge/release gates. The project execution-role definition is versioned in the existing Definition Registry; historical execution references remain unchanged.
