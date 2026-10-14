# Lossless provider notification batches

**Draft contract for #1307.** The planner exists on the implementation branch;
native admission, durable batch lifecycle, per-original outcomes and inspector
integration are not enabled by it. Delivery/remaining acceptance is tracked in
[#1307](https://github.com/garnser/codex-web/issues/1307), not this document.

## Planning boundary

`NotificationBatchPlan` schema1.0 stores complete immutable original request
snapshots and separate current queue-route snapshots as canonical JSON strings.
It fingerprints tenant scope, all originals and all routes. Decoding validates
unique unchanged IDs, compatible execution/reply scope, schema and context size.
Detached member views cannot mutate stored originals. Native input includes each
full original once, with current route pointers, rather than duplicating message
bodies in both projections. No source body is truncated to fit a batch.

`select_notification_batch` plans around the existing admission anchor. It uses
complete retained preflight attempts and separately attested provider-notification
IDs. Those IDs must come from authoritative provider-event provenance; source
strings, message parsing and caller task text cannot supply the attestation.
The planner does not retrieve/establish that proof or grant authority.

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
