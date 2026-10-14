# Lossless provider notification batches

The admission foundation is tracked by
[#1416](https://github.com/garnser/codex-web/issues/1416). It provides a planner,
preflight member-claim ledger, guarded scheduling coordinator, native submission
path and owned-drain adapter. The application installs that adapter through
canonical configuration and authority services; its feature flag defaults to
disabled. Complete batch lifecycle and per-original
outcomes and live qualification remain tracked in
[#1307](https://github.com/garnser/codex-web/issues/1307).

## Planning boundary

`NotificationBatchPlan` schemas1.0/1.1/1.2 store complete immutable original request
snapshots and separate current queue-route snapshots as canonical JSON strings.
It fingerprints tenant and current effective authority scope, all originals and
all routes. Decoding validates
unique unchanged IDs, compatible execution/reply scope, schema and context size.
Detached member views cannot mutate stored originals. Native input includes each
full original once, with current route pointers, rather than duplicating message
bodies in both projections. No source body is truncated to fit a batch.

`select_notification_batch` plans around the existing admission anchor. It uses
complete retained preflight attempts and separately attested provider-notification
IDs. Those IDs must come from authoritative provider-event provenance; source
strings, message parsing and caller task text cannot supply the attestation.
The planner does not retrieve/establish that proof or grant authority. A separate
current effective-authority fingerprint is required for every member; identical
profile IDs alone cannot establish equal Work Item/resource permissions. Native
admission must resolve and recheck that authority, not trust a submitted hash.
The attested provider source is caller-supplied data, not a code-owned provider
catalog. Existing direct-human `web`/`slack` intake protocols are forbidden as
notification origins even if a caller incorrectly includes their IDs.

New internally dispatched GitLab state notifications may carry a typed
`ProviderNotificationPin`: exact canonical event ID/checksum/source, tenant and
Project, provider event kind, Work Item reference and generated-message checksum.
Notes and items with handoffs are excluded from pin creation. The queue serializer
omits absent pins, preserving ordinary shapes. Event-specific pins stay individual
provenance and do not change execution-scope compatibility.

The verifier reloads the exact canonical event and checks its kind, checksum,
scope and message binding. Missing/changed/foreign events or handoff/notes cannot
qualify an original. An old source label or message never gains a pin by inference.
Pinned requests are excluded from legacy destructive wakeup/notification
compaction and pin identity participates in queue deduplication. Legacy queue
save also preserves distinct pinned originals with identical projected messages.

Eligible originals have queued, unstarted retained attempts in the same tenant
and Project, exact matching immutable request snapshots, and pinned actor/Agent
Profile/execution profile. Explicit retries, native uncertainty/start receipts,
scheduling retirement and handoff requests remain separate. The current queue
projection may differ from its original snapshot only through the preflight
domain's verified replacement route and corresponding reply-thread projection.

Execution and reply scopes must match. Work-item/execution/request IDs, creation
times and full message bodies remain individual provenance. Reply metadata's
`updated_at` is preserved for each original but does not change destination
compatibility; every other reply field must match. Repository sets, profile
revision/actor, model/sandbox/approval policy and contextual bindings cannot be
merged into broader scope.

Caller-supplied planning bounds are constrained to2–16 originals and256–262144
UTF8 bytes, including the admission identifier envelope. These are structural
ceilings, not operational defaults or authority.
Runtime limits must resolve from current canonical published policy/profile/
Definition controls with exact attribution. Oversized/incompatible entries remain
ordinary originals, while later compatible entries keep queue order. A plan
neither mutates a queue nor claims admission, native startup, ACK or completion.

## Native integration boundary

The draft store uses existing keyed preflight namespace records: `b/` admission
descriptors and original `a/` member attempts. A plan/claim-derived admission ID
keeps released-claim history when the same originals are claimed again. All member
claims and the descriptor commit through one bounded `record_mutate` transaction.
Idempotent claiming rechecks every original snapshot and claim. Competing owners
cannot acquire different subsets of the same plan.

A prepared claim may release all members together. Before any future native I/O,
an atomic gate changes the descriptor and every member to native-outcome-unknown.
That state cannot release or use ordinary single-request update/put/retry paths;
it needs batch-scoped authoritative reconciliation. Member snapshots, requested
execution IDs and unrelated attempts remain unchanged. These operations do not
remove queue copies or submit native calls.

The draft scheduling coordinator durably captures original queue order before
claiming members, then performs all-or-none exact removal in the existing keyed
queue transaction. Caller-provided current authority/ownership checks and every
retained original/claim are rechecked in that mutation. Concurrent appends and
unrelated entries survive. A changed/missing member refuses removal and leaves
the prepared descriptor available for guarded recovery.

Prepared recovery atomically restores missing exact originals against surviving
order anchors without overwriting changed copies; concurrent explicit priority
remains distinct from ordinary arrivals. It then releases all claims. A crash
after restoration but before release leaves exact copies plus protected claims
and is safely repeatable. Lost ownership or a native-outcome-unknown descriptor
refuses this recovery. These operations are not enabled in production yet.

The optional `NotificationBatchDrain` adapter runs inside the existing owned queue
drain, around its existing admission candidate. It scans at most 256 entries from
that anchor, reloads canonical events, retains eligible originals and requires
trusted configuration and current authority callbacks. It rechecks the same
ownership fencing token, automation admission, policy and authority before queue
removal and native submission. Missing ownership, actor resolution, enabled
configuration or exact event proof leaves the individual path available. A failed
claim never releases a competing owner's claim. A held or uncertain batch never
falls through into a duplicate individual admission. The adapter is intentionally
connected to canonical runtime controls. Installation does not enable its
feature flag or satisfy the parent's operator/reconciliation qualification.

## Canonical runtime controls

The Configuration Registry supplies `execution.notification_batches.enabled`
(default false, with a deployment kill switch), `max_members` (default4,
structural range2–16), and `max_context_bytes` (default65536, structural
range256–262144) under the same prefix. Deployment, Organization, Workspace and
Project targeting use the existing resolver and administration UI. Configuration
never grants permission, starts a model, or globally resumes paused autonomy.

Before grouping, each original resolves a current canonical actor and Project,
its exact pinned Agent Profile through current invocation access, the published
execution profile and live Role authority. The Work Item must belong to that
tenant/Project and have no current handoff. Repository associations must converge
with requested scope; all involved repositories must remain active and
Project-bound. Coordinated scope cannot absorb unrequested repositories. Scratch
profiles preserve their absence of filesystem repository rights. A current
`api.threads.write` execution decision must allow the exact Project/resource set
and one admission; existing worker/model budgets continue through the ordinary
native path. The profile input-token ceiling also conservatively caps bytes.

Schema1.1 plans retain a frozen `NotificationBatchControlSnapshot`: exact
configuration record/revision/value pins, pinned profile record/hash, current
profile access decision, execution and authority Definition references, matched
authority grants/delegations, actor and Project scope, repository metadata, and
effective bounds. These determine the authority fingerprint and plan identity.
Source labels or an externally supplied fingerprint cannot create this snapshot
in the installed adapter. Changed or unavailable controls refuse grouping.

The captured controls and ownership fence are rechecked in guarded queue removal,
at the native uncertainty gate, and immediately before adapter input delivery.
A change before gate consumption leaves a prepared claim; a later change leaves
uncertainty held without replay. Selected UX context is included in the delivered
input byte ceiling. These checks do not reinterpret missing receipts as nonstart.

New readers accept both plan versions. Schema1.0 shapes omit absent control
snapshots and retain their original fingerprints. Automatically selected plans
use1.2 with the result protocol below; older extra-forbid plan readers reject the new shape. Membership and
provider-pin mixed-writer/drain/backup/rollback constraints still apply. No
production enablement, migration, deployment, per-original completion proof or
live throughput acceptance follows from installing the adapter.

The explicit submission path preserves the original anchor's actor, Project,
profile/revision, execution profile, repositories, reply destination, model,
sandbox/approval policy and contextual binding. Runtime checks these fields and
the exact retained input before consuming the batch gate. Consumption records
native-outcome-unknown before I/O; subsequent attempts cannot consume it again.

The existing native adapter's actual start response supplies one aggregate
`QueuedNativeStartReceipt`, bound to the admission/claim and thread. It is
captured before later context-delivery audit bookkeeping. The admission becomes
native-input-started; original attempts retain unresolved per-item outcomes and
receive no fabricated individual start receipts. Duplicate identical receipt
capture is idempotent, while changed or foreign receipts refuse mutation.
Recorded native input delivery is not per-item completion or handoff acceptance.

An affirmative typed upgrade-guard refusal before worker/session routing is
recorded as `prestart_refused`, bound to the same admission and all member claims.
It permits exact queue restoration and claim release while preserving the refusal
history. Only the internal before-worker guard supplies this witness; timeouts,
generic errors, absent receipts and later provider failures cannot classify a
batch as not started. Failed receipt persistence leaves native uncertainty held
and cannot trigger another submission.

Ordinary preflight serialization omits null batch membership, retaining its old
shape. Claimed members carry `notification_batch_id`; old extra-forbid preflight
readers reject that field rather than silently dropping ownership. The admission
descriptor is explicitly schema1.0. Ordinary queue rows omit absent provider
pins. Old queue readers ignore a present pin, so mixed writers are unsupported
once pinned events are emitted: they could compact those originals without proof.
No backfill or inference from old source strings is permitted.

This foundation performs no production migration or deployment. A qualified
deployment must stop every queue/preflight writer and native admission, verify a
canonical backup, install the batch-aware release on every writer, and verify
retained originals before resuming. Source-only rollback is supported only before
the first provider-pin or batch-membership write. After that boundary, rollback
requires a qualified backup restore with native-outcome reconciliation; draining
or deleting descriptors cannot make an older writer safe. New readers accept old
rows with absent pins/membership, without making them eligible for batching.

The existing turn queue, keyed preflight records and ownership/fencing primitives
must own batch admission and recovery. Complete originals must be durably retained
before any queue removal. A bounded atomic preflight mutation must claim all
members together; a queue mutation must recheck exact snapshots while preserving
concurrent appends and unrelated original order. Partial claim/removal failures
must remain recoverable without inventing native outcomes.

One batch native start has one actual provider receipt. Original requested
execution IDs remain attribution; they must not be fabricated into multiple
native submissions or receipts. Batch completion is not per-original success.
Per-original outcomes need explicit canonical receipt/Evidence provenance, and
uncertain/uncompleted originals remain held or safely recoverable. Historical
unknown requests cannot become eligible merely because their queue copies exist.

Persisted membership/lifecycle must extend the existing preflight domain with
explicit compatibility and old-reader refusal. Before writing any new durable
contract, qualified deployment must drain all applicable writers/native admission,
take a canonical verified backup, declare the rollback boundary and preserve
original requests and published lineage. An in-memory planning object alone is
not persisted batch compatibility or crash/restart qualification.

The queue/orchestration inspector must expose original count, membership, oldest
age, native admission/uncertainty and individual outcomes, with canonical payload
and scope/provenance links. Refresh must remain read-only. Live acceptance needs
actual useful throughput/oldest-actionable-age evidence from the original queue;
synthetic fixtures or a shorter displayed queue do not prove it.

## Read-only operator inspection

The thread preflight surface loads bounded notification admission history and
lets an authorized viewer inspect exact immutable originals and admitted routes.
It distinguishes prepared, affirmative pre-worker refusal, native uncertainty,
shared native input delivery and released claims. A shared receipt is shown once
as aggregate delivery evidence; member outcomes remain unresolved unless current
per-original canonical evidence and independent verification qualify below.
Missing or changed retained attempts are visible as integrity failures without
erasing their original descriptor snapshots or claiming successful processing.

`GET /api/threads/{thread_id}/notification-batches` returns schema1.0 summaries,
including original count, oldest original age, exact scope, phase, recovery state,
and a cursor. Pages are bounded to 50 admissions (20 by default). The detail GET
at `.../notification-batches/{batch_id}` returns every original and its original
requested execution ID, full body, actor/profile, repository and reply provenance.
Both reuse canonical thread/Project, tenant and API read authorization. Refresh,
pagination and detail inspection perform no admission/recovery or model calls.

New claims atomically add an index under `bt/` in the existing preflight namespace;
its prefix hashes the exact tenant/Project/thread tuple. Released history remains
inspectable after member pointers are cleared. This index is additive and does
not change the descriptor schema. Legacy unindexed descriptors remain directly
inspectable by their retained membership link or known admission ID; history
explicitly reports `indexed_admissions` and does not silently backfill on a read.
Any existing unindexed production history must be accounted for during qualified
deployment before live batch acceptance is claimed.

Batch-bound members expose no individual retry, pre-start reconciliation,
bootstrap recovery or scheduling-retirement controls. Their service entry points
also return a batch-reconciliation conflict before attempting those mutations;
the store's batch ownership fence remains authoritative. This inspection surface
does not enable runtime processing or qualify production throughput tracked by
#1307 and its #127 UI review.

## Per-original evidence inspection

Canonical Evidence metadata may carry `notification_original_outcome`, a JSON
string conforming to `NotificationOriginalOutcomeBinding` schema1.0/1.1. It pins the
admission ID, original ID, SHA256 of the exact retained immutable original JSON,
and a disposition of `reconciled` or `follow_up_required`. This is a code-owned
protocol binding, not a mutable operational definition or an authority grant.
The existing Artifact/Evidence APIs own production, review and invalidation;
there is no additional outcome ledger or write endpoint.

An eligible record is canonical `runtime_result` evidence in the original tenant,
Project and Work Item, bound to the shared receipt's actual execution ID, provider
and native turn (external ID), observed no earlier than native input delivery.
Reconciled evidence has result `pass`; follow-up evidence has result `warn`.
Evidence and any supporting artifacts must remain valid, active and unexpired.
The JSON binding alone never establishes a result. A canonical Verification must
target exactly that evidence, the same execution and Work Item, use method
`notification-original-outcome/1.0`, and be independently `verified` by a different
identity after evidence creation, distinct from both the producer and the
original execution actor. Self-review, general review and a broad review
covering several evidence records cannot substitute for this exact binding.

Inspection reads one canonical evidence snapshot per request. It exposes bounded
proof IDs, snapshot digests, blockers and counts on the existing history/detail
surface. Conflicting current dispositions or adverse independent reviews remain
unresolved; it does
not choose the most convenient latest record. Expired/invalidated evidence,
changed retained originals or member claims also remove a projected result when
no other current qualifying proof remains. Unreviewed, mismatched or inactive
metadata cannot revoke a separate current verified result; such records are
reported as warnings. Changed or missing retained claims always block the result.
Invalid or incompatible binding JSON cannot create an outcome. Foreign records
and other admissions are excluded without exposing their proof IDs.

`reconciled` means independently reviewed reconciliation of this notification;
it does not establish business acceptance, ACK, issue closure or permission to
replay. `follow_up_required` identifies reviewed remaining work and does not
enqueue or submit it. Inspection never changes an attempt status, member claim,
queue, native receipt, admission phase or authority decision. Shared telemetry
without the per-original binding and review leaves all individual outcomes
unresolved. Missing evidence support fails closed.

This additive inspection uses existing Evidence metadata and changes no persisted
batch schema. Older readers continue to show unresolved outcomes; rollback loses
only this projection, not proof or originals. The proof writing/review process
uses existing canonical API authorization and data-governance invalidation.
Automated proof production, processing/follow-up integration, deployment and real
Orchestrator useful-throughput qualification remain separate parent acceptance
requirements; this read-only view cannot fulfill them by itself.

## Structured native result capture

New canonically controlled plans use schema1.2 and pin `result_contract_version`
to1.0. Their native input includes a bounded machine-readable result contract and
each original's immutable snapshot digest. It does not duplicate original bodies.
The contract requires one final JSON object with schema_version, exact admission
ID and results for every original ID/digest. Each result proposes `reconciled`,
`follow_up_required` or `unresolved` with a1–1000-character reason. Final output
has a32768 UTF8-byte structural ceiling; existing profile/worker output and cost
limits still apply. The entire added input overhead participates in planning and
native byte admission bounds. Legacy plan1.0/1.1 inputs, omitted fields and
fingerprints remain unchanged; new plan identity includes the protocol version.

Native receipt persistence atomically indexes the shared canonical thread/native
turn under `bn/` in the existing preflight namespace. A conflicting receipt cannot
associate that turn with another admission. The actual terminal callback finds
the admission through that index. Receipt capture also checks for a terminal
result already observed before the SDK start response, handling that race without
another native input. Both use the existing runtime event/receipt paths; there
is no additional scheduler or model call. Collector failures remain observational
and cannot release member claims, replay input or prevent unrelated queue progress.

The collector reads the exact canonical provider/runtime/native-turn transcript,
requires successful completion and validates the entire final result before its
first write. Missing/ambiguous final output, duplicate JSON keys/original IDs,
changed membership/digests, malformed/oversized output, failed turns, foreign
scope and changed retained claims cannot produce proposals. Canonical history
preserves final/commentary phase and treats failed/interrupted native completion
as failed. If the source turn has not completed, capture waits for its terminal
event or an explicit reconciliation request; missing receipts never prove nonstart.

Each original proposal becomes canonical `runtime_result` Evidence through the
existing service and governance boundary, with binding schema1.1, actual execution
and provider/native-turn provenance, source output digest and deterministic
idempotency key. The registered capture service is the producer; native completion
does not create a Verification. Reasons stay in the authorized canonical native
transcript; Evidence stores a reason digest and fixed review-required summary,
avoiding a new copy of arbitrary output. Captured evidence denies model context.
Every captured proposal has result `info`. An `unresolved` proposal cannot become
reconciliation merely through review. Reconciled/follow-up dispositions require
the exact independent verification gate above. Captured proposal/reconciliation
records, including manually produced per-original outcome bindings, are excluded
from generic technical completion gates, including permissive
requirements accepting informational results; notification review never establishes
business completion.

Repeated terminal events and restarted capture return the same canonical evidence
IDs. A partial persistence failure can resume the remaining records; changed output
refuses to overwrite previously captured evidence. An administrator with MFA and
current `api.evidence.write` authority may explicitly reconcile capture through
`POST .../notification-batches/{batch_id}/results`, submitting only the expected
shared native turn ID. The caller cannot provide result text, disposition,
evidence, replacement route or native input. This resumes observational capture
from canonical state and never submits a model turn. The existing inspector shows
candidate evidence IDs, review blockers and canonical Evidence/Verification links;
reading history/detail remains read-only.

Binding1.1 adds the unresolved proposal disposition. Older outcome inspectors
reject1.1 and continue to show unresolved results rather than granting completion.
Plan1.2 is strict-reader incompatible; the existing full-writer drain, verified
backup and post-write rollback restrictions apply. The new native index is
additive and is not silently backfilled during inspection. Evidence/Verification
retain their existing persistence and data-governance contracts. Captured proposals
do not qualify independently verified processing/follow-up, human-facing reply
presentation, production rollout or real Orchestrator throughput acceptance.

## Guarded operator queue restoration

An administrator with MFA and current canonical `execution.retry` authority can
review an admission through its `/recovery` GET and return its exact originals
through the corresponding POST. The request contains only a reviewed admission
digest and a bounded reason. It cannot supply a native-outcome assertion,
replacement thread, profile, repository or reply override. Inspection remains
read-only; restoration never starts a native turn or resumes paused autonomy.

Only `prepared` or affirmatively `prestart_refused` admissions with complete
retained member claims and original queue-order provenance can restore. Missing
receipts, timeouts, expired claims, inactive markers and model assertions never
qualify `native_outcome_unknown` or `native_input_started` admissions. Their
unresolved originals remain held for authoritative reconciliation.

Before queue mutation, an atomic member/descriptor check stores an immutable
recovery reservation. That reservation fences ordinary release and native gate
consumption. Queue ownership, current administrator/Role authority, repository
bindings, canonical replacement route and the reserved descriptor are rechecked
inside guarded mutations. Exact restoration preserves concurrent arrivals and
human Slack priority. Changed queue copies refuse overwrite. Verified replacement
routing changes only internal thread/reply-thread pointers; original request
snapshots, IDs, bodies, timestamps and scope stay unchanged.

Final release atomically clears every member claim, records the recovery outcome
and projected replacement pointer, and adds inspection indices for the destination
thread. A crash after reservation or queue restoration leaves protected originals;
the same reviewed request can resume without duplicate insertion or native replay.
No per-original success, ACK or business completion is inferred from restoration.

Admissions with recovery use descriptor schema1.1 and a typed schema1.0 recovery
record. Legacy descriptor1.0 shapes omit absent recovery fields. New readers
support both; older strict readers reject1.1. Existing full-writer drain, verified
backup and rollback limitations still apply before production writes. This control
does not complete per-original native outcome reconciliation or live throughput
qualification required by #1307.
