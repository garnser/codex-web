# Lossless provider notification batches

**Draft contract for #1307.** The planner, preflight member-claim ledger, guarded
scheduling coordinator and explicit native submission path exist
on the implementation branch; automatic admission selection, complete batch lifecycle, per-original outcomes and inspector
integration are not enabled by it. Delivery/remaining acceptance is tracked in
[#1307](https://github.com/garnser/codex-web/issues/1307), not this document.

## Planning boundary

`NotificationBatchPlan` schema1.0 stores complete immutable original request
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
UTF8 bytes. These are structural ceilings, not operational defaults or authority.
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
refuses this recovery. These scheduling operations are not connected to native
automatic admission selection or enabled in production yet.

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

Ordinary preflight serialization omits null batch membership, retaining its old
shape. Claimed members carry `notification_batch_id`; old extra-forbid preflight
readers reject that field rather than silently dropping ownership. The admission
descriptor is explicitly schema1.0. Qualified full-writer deployment and declared
rollback/skew limits remain prerequisites before writing membership in production.

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
