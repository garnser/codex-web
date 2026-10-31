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

### Project-scoped coordination owner references

The existing `execution-roles.default` / `execution-role-catalog` Definition
Registry schema `1.0` owns mutable `owner_to_execution_role` mappings. A direct
Project-scoped mapping from a legacy owner label to an active coordination role
may resolve that label for routing. The target must map to itself. Canonical
role names, claimed implementation/validation/release owners, chained mappings,
cycles and conflicting normalized names cannot be aliased. Global mappings do
not activate coordination aliases. Canonical Project scope is attested before
using an alias, and each operation reads a fresh effective definition record.
There is no hard-coded Operator identity, new Operator profile, seeded alias or
installed configuration change. Authenticated principals and authority roles
are never passed through owner-reference normalization.

Intake retains original provider labels and owner fields. Unknown/unavailable
routing cannot infer implementation claims. Reads explain the configured label,
effective canonical role, scope and exact definition without changing receipts.
An alias selects only an existing exact-role, same-Project bot binding; it cannot
clone a binding, use fuzzy thread names, or select the first similarly named
profile. The thread's canonical bootstrap assignment supplies the actual actor
and Agent Profile. Current profile lifecycle/access, exact revision, role and
tenant must qualify; absent or changed provenance is an explicit blocker.
ExecutionProfile, RoleAuthority, resource, SecretBroker and live worker
lease/fence checks remain the authority for actual execution and actions.
The execution-role catalog's default execution profile is not an authorization
grant or a substitute for the actual published Agent Profile override.

New aliased handoffs retain an immutable `owner_routing` binding, schema `1.0`,
with configured and canonical recipients, tenant/Project and exact Definition
reference. An ACK must name the actual canonical actor, identify the exact
`expected_handoff_requested_at`, and have qualified actual native delivery.
Queue admission, a current alias or an old delivery without the original binding
cannot prove an alias ACK. Historical accepted recipients retain their original
binding even when the current mapping changes or disappears. Legacy accepted
handoffs retain their original raw recipient; a new mapping cannot retrofit an
old receipt. Existing unbound pending aliases require a governed new intent,
not automatic reinterpretation of potentially started work.

An aliased dispatch retains an `owner_routing_pin`, schema `1.0`, on the existing
QueuedTurn. It identifies that binding, Work Item, stage/retry/handoff generation,
original bot binding and thread. Existing queue fields retain exact actor,
AgentProfile revision, reply target and admitted repository scope; the pin adds
no grants or credentials. Pinned originals cannot be compacted with wakeups,
deduplicated by text alone or batched as provider notifications. They enter the
ordinary retained preflight/owned queue drain, including idle-thread dispatch.
Direct queue steering preserves a pinned original and returns
`owner_routing_guarded_queue_required` before interrupting or starting a turn;
that path has no retained native-start claim and cannot reconstruct the request.
Current definition, owner, generation, binding/profile and verified replacement
lineage are checked after awaited recovery and again under the native writer
lock. Unsupported guarded transports refuse before input. Authoritative
pre-input refusals retain the original for inspection; unknown or positive
native outcomes retain their existing reconciliation semantics and never
authorize replay. Actual start recording uses the original bound recipient,
not the mapping effective after the native input was sent.

Repository handoffs may retain an explicit owner-routing pin `1.1` when the
conversation-bound lane has incompatible native repository custody. Selection
uses the existing tenant-scoped bootstrap bindings, indexed assignments,
current AgentProfile/role and worker lease: exactly one live compatible lane
created by the same requester must qualify. Missing, ambiguous, stale, revoked
or quarantined recipients produce the existing visible Work Item routing blocker
before any source-lane retirement. Scratch-only coordination profiles retain
their existing no-repository route. Selection performs storage work off the
async event loop; it neither creates threads/bindings nor grants access.

An equal writable scope on the bound assignment does not bypass this selection
when the scoped canonical replacement graph already names a successor. The
replacement relation grants no execution authority: the existing unique live
recipient and final custody checks still apply. Without a replacement relation,
ordinary first admission and same-thread bootstrap/recovery retain their existing
path, including when no live recipient has been established yet.

The `1.1` recipient witness identifies actual bootstrap, assignment, execution,
worker/fence and a checksum of immutable authority fields. The original bot
binding/thread and reply target remain conversation provenance, including for
an execution recipient without its own Slack binding. Queue status and native
delivery identify the actual recipient thread. Current Work Item repository
scope, original source binding eligibility, actor/Profile/Definition authority
and current recipient custody are rechecked before native input. Lease renewal
clocks and append-only procedure discovery are not custody changes. A change
requires a new governed intent; uncertain originals cannot be redirected.
Legacy `1.0` pins serialize exactly their original shape without a recipient
field, preserving retained snapshot comparison. Actual matching native delivery
is still required before ordinary recipient ACK; routing itself never ACKs.

After intake is accepted, GitLab provider callbacks to the current owner use
the same compatible-recipient selection and native custody witness. The
original conversation binding remains reply provenance; the provider event pin
continues to attest the exact canonical event and message. Both pins remain on
the ordinary retained single input, without relaxing provider-notification batch
eligibility. Missing, ambiguous or changed custody records the existing visible
owner-routing blocker rather than dispatching to the incompatible bound thread.
This callback does not create a new handoff or ACK, and nonterminal pipeline
progress still records deterministically without selecting or starting a model.

A committed native-start observer also projects exact late positive delivery
after a waiter times out or an earlier metadata projection fails. It reuses the
canonical snapshot-digest/correlation checks, exact response evidence and
receipt, and the original immutable AgentProfile revision's role. Current alias
or profile revocation cannot rename or erase that historical fact. The observer
does not renew a lease, grant authority, replay input or ACK the handoff. Failed
metadata projection leaves the native fact committed; the existing authorized
native-evidence reconciliation can retry the idempotent projection.

The existing preflight collection also maintains a derived `wi/` scope/ref index
(projection schema `1.0`). Startup backfills old authoritative `a/` records in
200-record pages with a resumable readiness marker; it does not rewrite their
snapshots or outcomes. Current writers commit index entries before queue removal
or native start. Alias admission requires a ready index and a complete bounded
200-entry lookup, then rechecks authoritative records. Any unresolved original
for the same tenant/Project/Work Item blocks a fresh alias dispatch, including an
old unpinned original on another thread. Scheduling retirement does not settle a
native outcome. At native admission only the exact current pinned reservation
can be excluded; its own claim/fence still crosses the ordinary guarded send.
Unavailable or truncated lookup fails visibly. This index is not a new queue,
permission store or outcome authority, and requires the same writer drain.

The optional WorkItem `owner_routing_blocker`, schema `1.0`, records one current
configuration denial without overwriting execution failures, lane claims or
substantive prerequisites. Repeated identical denials add no routing-failure
events or native requests. A qualifying queued dispatch clears it only while
its original intent is still current. Reads remain observations, not grants;
alias steering additionally requires the exact `expected_definition` from the
current `owner_dispatch` projection. The existing Work Item inspector shows
current routing, actual profile selection, denial/repair and historical binding.

Absent bindings, pins and blockers are omitted during serialization, preserving
legacy shapes and retained snapshot hashes. New readers accept old rows; nested
unknown routing schema versions/fields fail visibly. The turn queue itself has
no version envelope and old queue/Work Item readers may ignore these optional
fields. Mixed writers are therefore unsupported after the first routing-field
write. Before activation, stop and drain **all** Work Item, queue, preflight and
native-admission writers, take and verify a canonical backup, install compatible
readers/writers everywhere and qualify retained originals before resuming.
Source-only rollback is supported only before the first routing binding, pin or
blocker write; afterwards it requires a qualified backup restore plus native
outcome reconciliation. Deleting pins or draining descriptors is not a rollback
proof. Source tests do not establish installed original-work delivery or ACK.

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

A nonempty owner thread queue is not proof that the current request is queued.
`already_queued` requires an exact current canonical prompt (including its
artifact/result context), Work Item, Project, repository read/write scope and
handoff timestamp. An unrelated, stale or unreadable retained request returns
`owner_queue_context_mismatch` or `owner_queue_context_unavailable`; a concurrent
drain returns `owner_queue_context_changed`. These outcomes preserve every
original request and FIFO order, start no native work and grant no delivery or
ACK evidence. The actual dispatch repeats the identity check at its idle-queue
race boundary. The Work Item inspector exposes the queue outcome and repair
action, while a matching queued request remains explicitly delivery-unproven.

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

A typed owner-recipient witness preserves the recipient's **already-admitted**
read-only repository context when its writable repository set exactly matches
the Work Item. Dispatch revalidates the current assignment and its full authority
checksum before deriving this read-only context; caller-supplied repositories
are never unioned into it. A mismatching supplied scope is refused. The retained
queue and final native admission carry the exact read/write sets, and a changed
recipient scope, actor, Profile, lease or fence invalidates the witness before
input. This also applies when the source channel binding already points to the
correct recipient. It neither changes that binding nor grants new access.


### Original native custody during recipient selection

A current bootstrap's repository scope does not establish the custody of an
already existing native session. Owner routing reads the bounded canonical
native-session aliases and the session's original assignment. Ambiguous,
foreign or missing original custody refuses selection. An incompatible source
cannot take the equal-current-scope shortcut, and an incompatible candidate
cannot pass the final recipient witness validation. The native session custody
owner remains the final authority before provider resume.

For a pending structured handoff, routing first reuses one compatible LIVE
recipient. When none exists specifically because the original native scope is
incompatible, the existing Thread service may prepare one new context under the
original current Profile, actor and exact Work Item repository scope. Its
requested scope is compared separately from the current bootstrap: original and
current custody agreeing on repository A does not make them compatible with a
handoff requesting repository B. Absent native custody or an unavailable
same-scope LIVE lane does not itself authorize fresh-context preparation. Its
bootstrap token is deterministic for the original binding, Work Item and
handoff nonce. The existing canonical bootstrap assignment reserves the attempt;
if that assignment exists without a positive binding, subsequent observations
report pending and never repeat provider creation. Local queue ownership and
replicated fencing serialize preparation. Creation is not delivery: only a
subsequent original native START qualifies the handoff. Existing source sessions,
bindings, replacement graphs, queues and unknown outcomes remain unchanged.

A positive original native session whose historical assignment is missing remains
unverified history. It cannot authorize original resume or new context creation.
When the original source is terminal and unleased, its tenant-scoped canonical
replacement may instead supply current custody: the replacement must have an
exact native-session/assignment/execution/workspace/worker match, the same creator
and pinned Profile, and the requested repository scope. Existing current worker,
lease, fence, policy and unique-recipient checks still apply. Ambiguous or foreign
original custody, unresolved original Work outcomes, missing or cyclic replacement
lineage and multiple current candidates still refuse. The final native guard
rechecks the lineage and custody; no original binding, session, assignment, nonce,
history or authority is rewritten and no replacement is created by this lookup.

A genuinely pending, unacknowledged, undelivered and unexpired handoff may also
prepare fresh context for positively known cancelled recipient custody. The
source must have an exact READY native-session/current-assignment association,
no lease, the same creator and full Profile, exact requested repository scope
and current policy. Missing historical custody requires its existing scoped
canonical replacement; absence, arbitrary matching lanes and reason strings are
not cancellation proof. Preparation reuses the same Work/nonce/binding bootstrap
reservation and existing pre-worker recovery cap. It never resumes the cancelled
assignment or rewrites a source binding, Session or replacement graph.

Subsequent selection and the final native edge require the exact nonce-reserved
bootstrap and fresh LIVE recipient authority. The final edge retains the
caller's exact own-original admission exclusion rather than adding a second
unresolved-input gate. Existing periodic SLA observations can reconsider the
same pending nonce through admitted canonical routing after readiness changes;
its cooldown, current-lane busy checks and original expiry remain effective.
Neither a handled routing refusal nor context creation proves native delivery.

After a prepared recipient has actually started and accepted its current handoff,
current-owner routing may read that existing lineage without preparing another
context. The accepted handoff must retain its qualified delivery and exactly one
matching canonical original with positive START correlation, evidence and receipt.
The original Work, tenant, nonce, binding, actor, Profile, routing Definition and
repository scope must agree. Its deterministic bootstrap reservation is required;
a generation-one reservation additionally requires the existing controller-owned
pre-worker refusal and unchanged predecessor attribution. Missing, ambiguous or
unknown originals do not become permission through an ACK or delivery timestamp.

Selection and the final native edge revalidate current worker custody, lease,
fence, Profile, actor, policy and repository scope. They construct a fresh owner
witness after ACK; the original pending-handoff owner checksum is historical
attribution, not current authority. The final edge excludes only its own exact
retained current-owner admission, while other unresolved originals still block.
This read-only path never creates a thread, replays an input, resumes cancelled
custody, extends the old nonce or edits source Sessions, bindings or history.
Pending-handoff preparation retains its original unacknowledged, undelivered and
unexpired guards. Ordinary accepted handoffs without a prepared reservation keep
their existing routing path.

If a current recipient admission callback raises a typed routing denial, the
recipient owner restores that exact exception from the controller's genuine
pre-provider thread refusal, preserving canonical blocker/event handling
(#1802). Exception-object identity must match the callback of that invocation;
a matching string, class or adapter-supplied wrapper is not evidence. Other
Python errors, existing HTTP refusals and post-provider uncertainty retain their
original handling. This neither weakens the current-intent/authority fence nor
retries an input, extends a nonce or asserts delivery/ACK. The existing operator
blocker and event surfaces display the existing canonical reason codes.

A controller refusal after canonical preparation but before worker startup is
recorded atomically with cancellation as an existing `FailureRecord`. The owner
must still hold an unclaimed PENDING fence0 with no lease, worker or start time.
The internal proof records the pre-worker/provider boundary, exception type,
HTTP status when present, and original recipient-attribution digest. Public
cancellation reasons cannot create this proof or attach it to historical
cancellations. A timeout classification does not imply the exact cause of a
historical refusal; the owned deadline flag is separate from an HTTP504.

The same genuine pending handoff may reserve one deterministic recovery
bootstrap only from that typed proof and unchanged tenant, actor, Profile,
definitions, source custody and repository scope. Its cancelled predecessor is
preserved, not resumed, and is freshly rechecked at every recovery edge. The
existing canonical assignment reserves the new physical generation. Unknown
outcomes, legacy reason-only cancellations and a failed or unresolved recovery
generation remain blocked; no third generation or new nonce is manufactured.
The Work Item inspector exposes `owner_recipient_bootstrap_recovery_pending`;
the assignment API retains the structured failure phase, Work/nonce, actor and lineage.

Provider-edge checks freshly read exact current authority and custody while
avoiding repeated discovery of unrelated candidates. Initial and owned-entry
discovery remain, and final selection still requires exactly one compatible LIVE
recipient before work is dispatched. A legitimate concurrent lane may therefore
make final selection ambiguous; context preparation is not Work START or ACK.
No five-second limit is increased and no authority snapshot crosses an await as
a substitute for fresh validation. These additions use existing assignment and
Failure contracts, with no new store, controller, public mutation or UI grant.

This changes no UI-owned eligibility or control. Existing Work Item handoff,
worker/bootstrap and preflight inspectors expose the canonical recipient or
blocker; current denial reasons remain authoritative. No new UI action is
required.

### Background handoff routing refusals

A handled owner-routing refusal from structured handoff dispatch emits the existing
`work_item_handoff_dispatch_failed` runtime event, including the attempted
handoff's original timestamp, recipient, source, and bounded reason code. This
remains observable when a concurrent Work Item update prevents the canonical
blocker compare-and-set. It does not overwrite the current handoff, retry input,
or establish a native input outcome. Coordinator completion means the background
invocation finished; it does not prove delivery. Delivery and acknowledgement
continue to require their existing canonical receipts.

### On-demand pending handoff routing inspection

The existing Work Item inspector offers **Inspect handoff routing** for a pending
handoff. Its scoped GET `/api/work-items/{ref}/operator/handoff-eligibility`
requires the observed `expected_requested_at` and `expected_recipient`. It runs
the structured dispatcher’s existing binding selection, synchronous
`compatible_pin` and fresh `context_for_pin` checks. It does not run the async
recipient preparer, create a bootstrap, record a blocker, enqueue input, retry,
dispatch, or acknowledge a handoff. A changed Work snapshot returns `changed`;
missing storage or routing composition is `unavailable`, never an eligible
default. Inspection first checks that existing Work, worker and native-session
record layouts are initialized; it does not trigger their lazy migrations.
Lookup identity uses the existing active, unambiguous local human and current
memberships with the original local-trusted lookup assurance; it cannot bootstrap
or restore a missing, disabled or revoked identity. The trusted read-only getter
is passed through the existing pin/context checks, never supplied by a request
or cached across checks. Readiness, scope lookup and routing execute off the
request event loop.
The original tenant, Project, Definition, Profile, repository and
retained-original checks remain authoritative.

This is a current routing snapshot, not admission or native START authority and
not a reconstruction of an earlier refusal. A valid pin may still require
ordinary worker/native admission; scope incompatibility is reported without
creating a replacement. Existing ordinary Work/operator and Executive context
reads do not invoke this potentially expensive inspection. The actionable-owner
projection retains its existing `work_item_handoff_pending` semantics.
