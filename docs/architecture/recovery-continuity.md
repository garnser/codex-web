# Production recovery, backup and continuity

## Status

Canonical production recovery contract. A backup is not considered recovery evidence until
codex-web can decrypt it with the canonical key boundary, reconstruct its
canonical state snapshot in isolation, validate integrity/dependencies and
produce a successful restore-verification record.

## Recovery objectives

RecoveryPolicy declares explicit RPO/RTO objectives by data class and deployment
mode. The default local objective for canonical state and audit is one-hour RPO
and four-hour RTO. Operators must intentionally configure the backup key and
destination before the system reports recovery qualification.

Recovery health derives from facts rather than a "backup enabled" flag:

- age of the latest encrypted backup versus canonical-state RPO;
- age and PASS status of the latest restore verification;
- measured restore-verification duration versus canonical-state RTO;
- configured backup-destination health.

Fresh backup plus fresh successful restore verification plus measured RTO are
required for recovery qualification.

## Backup boundary

A backup snapshot is a consistent StateStore document snapshot. This covers the
canonical database-backed control plane across SQLite and PostgreSQL:
configuration/policy/definition state, canonical events, approvals,
ActionIntents, Resources, Evidence metadata, organizational memory metadata,
release/incident state, extension/worker configuration, audit state and other
StateStore-backed domains.

Recovery policy, owner scope and policy-change history are included in the
snapshot. Recovery's backup/verification manifest collections are excluded to
prevent recursive backup growth.

Snapshot and envelope serialization retain compact, sorted-key ASCII JSON bytes.
Container traversal uses incremental encoding so an offloaded operation can
yield interpreter time between chunks. StateStore checksums retain their exact
namespace/NUL/document-digest/NUL framing and hash encoded chunks without another
complete document buffer. Encryption still receives the complete canonical
plaintext; key context, backup format and persisted checksums are unchanged.
This is an encoding implementation change, not a new recovery schema or a
different integrity algorithm.

Capture serializes snapshot metadata separately from the already JSON-compatible
StateStore documents, avoiding a recursive Pydantic serialization copy of the
catalog. Decoded documents are released before encryption; restore likewise
releases envelope/plaintext buffers before subsequent decoding and validation.
Isolated restore sanitizes and writes one namespace at a time instead of copying
the whole decoded catalog. Canonical JSON accumulation exposes its completed immutable buffer without
copying a full bytearray; encryption releases binary ciphertext before materializing
its base64 text. Memory regressions measure both phases independently.
These reduce overlapping representations; they do not
make capture constant-memory or remove the need to size the service for the
decoded state and complete authenticated encryption payload.

Transport/broker queues are not backed up as business truth. After restore they
are reconstructed from canonical state/outbox semantics.

Artifact/evidence **metadata and protected content pointers** are part of
canonical state. Bytes owned by an external artifact-content backend remain a
recovery dependency of that backend and must have its own durability policy.
Likewise secret/key material stays in its configured secret/KMS/HSM backend;
the snapshot contains references and version metadata, not plaintext secrets.

## Encryption and key dependencies

Backups require a canonical ManagedKey whose purpose is BACKUP. Recovery calls
CryptoKeyService with a tenant/workspace-bound EncryptionContext:

- object type: recovery_backup;
- object ID: exact Backup ID;
- organization/workspace: exact backup scope.

The encrypted envelope records key ID/version and authenticated context. Backup
payloads cannot silently decrypt under another tenant/workspace or object scope.

The snapshot also freezes the usable key-version inventory from that exact
canonical key-state snapshot. Revoked tombstones remain historical metadata but
are not prerequisites for a new backup. Existing backup manifests are never
rewritten when a key changes. Restore
verification calls CryptoKeyService.validate_manifest, so missing/revoked key
versions fail recovery qualification rather than being discovered during an
emergency.

No ad-hoc backup password/static encryption secret is introduced.

## Destinations and retention

BackupDestination is provider-neutral. The default LocalBackupDestination is
create-only, fsyncs encrypted envelopes and exists for simple/local deployments
and tests. Production/off-host/off-site adapters can implement the same put/get/
delete/health contract without changing RecoveryService.

Retention is count-based and deterministic. Expired manifests and their
destination objects are removed together.

## Integrity and audit continuity

Each BackupManifest retains:

- StateStore backend and schema version;
- namespace count/list;
- canonical document SHA-256 checksum;
- encrypted-envelope SHA-256 checksum;
- exact backup key ID/version and key manifest;
- autonomy-audit root/checkpoint present at backup time;
- recovery-policy ID/version/fingerprint.

Capture checks both current live audit integrity and the audit document in the
exported StateStore snapshot through the same chain/checkpoint/signature verifier.
The manifest's audit root and checkpoint come from that verified export, never
from a later live tail. Valid concurrent appends or checkpoints cannot change the
captured anchor; corruption in either the live audit or exported audit refuses
capture before destination publication. The audit verifier uses records and
checkpoints from one decoded state rather than separate live reads.

Restore verification authenticates/decrypts the envelope, recomputes state
checksum/count, checks exact StateStore schema compatibility, validates key
references and verifies the backed-up autonomy-audit root against the root
recorded in the manifest.

Publishing a verification commits its RestoreVerification, linked Evidence,
governance metadata and any successful scheduled-firing settlement in one
canonical transaction. Recovery retains the prepared Evidence creation's full
domain set and rechecks current keys, manual operator identity and scheduled
policy/admission under that transaction. A failed write or cancellation rolls
back all of these records together. Reconciliation's separate Evidence hook
continues to inspect locked existing records without creating new Evidence.
The existing Recovery API and operator result surfaces expose the same linked
verification; this repair adds no new operator control or schema boundary.

When configured, an invalid live autonomy audit also blocks creation of a new
"healthy" backup.

## Isolated restore and side-effect safety

Restore verification is intentionally isolated and reports
side_effects_enabled=false.

When a snapshot is materialized into an isolated StateStore, codex-web
additionally sanitizes executable control state:

- autonomy mode becomes paused;
- active Scheduler records become paused and lose leases;
- pending/claimed/executing/uncertain ActionIntents become
  requires_reconciliation and lose worker leases.

Therefore a successful storage restore cannot automatically resume provider
actions, scheduled automation or autonomous execution. Reconciliation and
operator authorization must happen explicitly before protected processing
resumes.

The normal HTTP API does not expose a blind overwrite-live-database operation.
A live destructive restore/failover needs an explicit target and the guarded
approval/compatibility orchestration defined by the production deployment and
safe-upgrade contracts. This avoids turning disaster recovery into an
unreviewed remote destructive action.

## Scheduled proof

Configuring RecoveryPolicy installs two recurring canonical Scheduler entries:

1. encrypted backup creation at the configured backup interval;
2. restore verification at the configured verification interval.

Both use normal schedule.due events; there is no private timer loop.

The restore-verification job publishes canonical POLICY_EVALUATION Evidence with
source recovery-restore-verification. PASS Evidence is consumable by the canonical
RECOVERY production-autonomy qualification gate.

## Failure scenarios

The contract makes the following failures visible rather than silently
recoverable:

- encrypted object corruption/tampering -> envelope/checksum failure;
- wrong tenant/workspace -> encryption-context failure;
- missing/revoked KMS key version -> key-manifest failure;
- incompatible StateStore schema -> compatibility failure;
- broken autonomy audit continuity -> audit failure;
- backup destination outage -> degraded recovery health;
- stale backup/restore drill -> RPO/verification failure;
- restore slower than objective -> RTO failure;
- stale side-effect state -> paused/reconciliation state after materialization.

The safe-upgrade/version-skew contract owns broader application/worker/extension migration
orchestration. Recovery records exact schema/policy metadata so those checks have
a deterministic input instead of guessing from backup age.

## API and UI

/api/recovery provides policy configuration, backup creation, restore
verification and recovery health/history. Human mutations require administrator
authority + MFA; service callers require recovery:admin.

The Autonomy Control Center and operator workspaces present backup age, last verified restore,
RPO/RTO status, destination/key/audit dependencies and guarded drill/restore
operations. Operator runbooks document the corresponding recovery procedures.

Key-reference creation and key retirement share an atomic StateStore update
across recovery and key metadata. Backup destination bytes are written before
manifest registration; if a referenced key becomes revoked before registration,
registration fails and the new encrypted candidate is deleted. Policy changes
likewise reject a key revoked after initial validation. Retained backup envelope
versions and frozen key-manifest requirements prevent retirement even after
rotation. Recovery retention/migration, followed by restore verification, owns
removal of those requirements; key administration cannot bypass them.

## Recovery policy administration

The Operations page separates typed RecoveryPolicy settings from read-only
backup/verification evidence. The policy is effective operational configuration
in the existing RecoveryState, not a reusable template or a second Definition
Registry. Mutable policy values remain data; validation, transaction engines,
cryptographic requirements and unsupported capability constraints remain code.
No model reasoning or new external provider execution path is introduced.

Metadata responses expose the current policy, exact fingerprint, immutable
policy-change records, actor/time provenance, registered destination IDs,
capabilities and the typed schema. Optional Project context is validated but
does not turn the shared recovery policy into a Project-owned copy. Policy scope
is bound to organization/workspace, including legacy ownership derived from its
existing backup key. If legacy key metadata is missing, ownership cannot be
inferred; recover that metadata before adopting the policy. A configured recovery
execution identity must belong to the policy's tenant.

Changes require the existing canonical recovery-admin authorization and human
MFA (or the service scope), plus the key boundary's authority. The editor uses
`POST /api/recovery/policy/preview` and supplies its current fingerprint to
`PUT /api/recovery/policy?expected_fingerprint=...`. A stale fingerprint rejects
the mutation with 409. Legacy API callers may omit the optimistic precondition;
all writes still use the same canonical scope, key and transaction checks.
Changing the policy's stable identity, enabling unsupported point-in-time
recovery, or disabling the engine's key-manifest capability is rejected. The
backup key must be workspace-scoped because the recovery envelope protects a
physical canonical-state snapshot, not an individual Project export.

Policy publication atomically persists its immutable change record, validates
key references and replaces both canonical schedule records. Old records are
cancelled with incremented revisions and cleared leases; paused/cancelled timers
remain paused in their replacements. Scheduler wake-up follows commit. Stale
queued schedule IDs, tenant scope and policy fingerprints are rejected before
execution. Operations already running are not reversed. Recovery objectives,
retention and intervals apply to subsequent operations without application
restart. Destination registration/backend configuration and worker topology
remain deployment settings. Retention only considers backup records in the
acting organization/workspace; another tenant's retained backups are untouched.

`POST /api/recovery/policy/rollback/{change_id}` requires a current fingerprint,
revalidates the retained policy and its current key/destination dependencies,
and records a new policy change attributed to the restored revision. This is
**policy-only rollback**. It cannot restore expired backup bytes, undo completed
operations, revive revoked key material, or roll back an application/schema
upgrade. Historical policies are not active key consumers; attempting to restore
one whose key was retired fails explicitly.

The UI requires impact review before publication/rollback, explains retention
and schedule effects, retains invalid/conflicted drafts, protects dirty
navigation and fences late Project responses. Backup and policy consumers link
to key metadata, and key dependency rows link back to the exact recovery record.
Stored destination paths, envelope contents and secret/key material are omitted
from the UI. Missing/revoked-key restore findings remain visible as evidence.

### State compatibility

Recovery state `1.1` adds immutable policy-change records and policy owner scope.
State `1.2` adds scheduled firing receipts and two replay watermarks. The explicit
`1.0 → 1.1 → 1.2` migrations preserve all existing policy, backup,
verification and schedule data. Unknown future versions fail closed. Legacy
ownership is adopted only from canonical key metadata, never request text. Once
`1.2` has been persisted, application rollback requires an engine supporting
that contract; older binaries must not be assumed able to read the new fields.
Policy rollback does not downgrade this state contract.
Before the first new write, stop and drain every older recovery writer. This
source migration does not provide mixed-version writer fencing or automatic
downgrade, and is not evidence of an installed upgrade.

Policy history survives isolated restore without recursively including old
backup/verification manifests. New backup key requirements come from the exact
frozen key document and exclude already revoked versions; retiring an unused
reference therefore does not make every future backup impossible. Revocation
racing with backup creation still fails the atomic reference-creation check.
Legacy policy adoption uses the transaction's locked key document rather than
opening a nested StateStore connection.

### Scheduled work ownership

The canonical Scheduler and event bus still own firing and dispatch. Recovery
offloads its synchronous admission, snapshot, encryption, destination I/O,
verification, retention and result writes to one owned executor thread. The
executor admits at most one active and one waiting operation; it does not submit
an unbounded thread-pool queue. Request actor ContextVars cross this boundary.
Direct synchronous recovery methods retain their existing caller-owned contract.

Before effects, a transaction across RecoveryState, keys and Scheduler validates
the actual stored canonical envelope, source, tenant/workspace, execution actor,
current active schedule, FIRE_ONCE policy, occurrence and exact policy
fingerprint. It reserves the backup/verification identity and, for verification,
the exact backup. Policy, authority, keys and schedule are checked again before
effects and under the transaction that admits the result. Scheduled retention
uses the same locked current policy; a superseded policy cannot delete backups
under its old retention limit.

Firing receipts distinguish `running`, `succeeded` and `unknown`. Success means
the actual operation returned and recorded its canonical result; a completed
verification can still report FAIL. Exceptions after admission record unknown
with the reserved identity and an error class, without snapshot or secret data.
A cancelled asyncio observer neither cancels the worker future nor releases its
custody. Cancellation propagates only after the actual operation terminates.
Shutdown closes admission before cancelling Scheduler/event tasks, drains both
already accepted jobs (including a queued verification), and then shuts down
the executor. Cancelling a queued observer cannot discard its committed event.
Restart of the same service object is
allowed only after this drain.

Redelivery never executes an admitted occurrence again, including after unknown
completion or restart. Across replicas, a running receipt blocks another firing.
A crashed owner's running receipt is not reclaimed by lease expiry or inferred
from an existing manifest: retention/evidence may still be running. Qualified
operator recovery must establish quiescence and reconcile the reserved effect;
there is no automatic clear, replay or inverse operation. Terminal unknown work
does not prevent a distinct future occurrence until the receipt bound is reached.

At most 128 receipts are retained. Successful receipts can be compacted because
the two current-schedule watermarks continue to reject their redelivery; replaced
schedules fail current-schedule admission. FIRE_ONCE also coalesces older delayed
occurrences behind an admitted watermark. Unknown receipts are never evicted to
make room; exhaustion and concurrent ownership report backpressure. Shared event
transport may retry a delivery that never admitted work. Local canonical ingress
does not replay an already inserted event, so a failed local delivery must not
be mistaken for a successful backup. No retry poller is added.
Legacy schedules without an exact policy fingerprint fail admission; an explicit
publication of the current policy repairs their timers through the existing
policy control transaction, preserving inactive state.

Snapshots preserve these bounded replay barriers, including historical running
ownership. Isolated restore still pauses schedules and autonomous execution;
restoring a snapshot does not confer authority to resume a lost owner's work.
Existing recovery status and policy UI expose canonical firing scope, event,
schedule, policy fingerprint, owner, reserved identity and uncertainty. Local
executor status is explicitly process-local, not proof of another owner's state.

### Verification settlement

Restore PASS Evidence, its RecoveryState verification and scheduled firing success
commit in one canonical multi-document transaction with the complete frozen key
manifest and envelope versions, current policy, schedule and firing custody.
Evidence preparation does not publish PASS. A committed success survives a lost
observer response; an admission conflict leaves the firing unknown without replay.
Existing Recovery/Evidence surfaces retain the canonical evidence/verification IDs
and denied or unknown outcome; no separate dashboard or authority is introduced.

Canonical KeyUsage retirement checks include every backup manifest key/version in
the shared Key/Recovery transaction. Auxiliary-key and non-current-version
revocations are refused while such consumers remain; this is positive protection,
not an executed revocation attack. Final frozen-requirement validation remains
defense in depth. Policy/schedule admission is independently rechecked at the
Evidence/verification success transaction.

### Explicit scheduled-owner reconciliation

Recovery state `1.3` extends `1.2` with at most 128 immutable reconciliation
records bound to retained firing identities. Explicit activation preserves prior
records/watermarks. A `1.2` writer must refuse `1.3`; drain all older writers
before the first write. There is no source-only rollback after activation.

The opt-in `recovery.scheduled-firing.reconcile` ActionProvider invokes the same
Recovery service and transaction. It does not register/activate itself or grant
its capability. Existing canonical ActionIntent authority, security, procedure,
lease and actor admission remain required. The provider's actor resolver must
use current canonical identity/session/authority state, never caller metadata or
a cached actor. The service repeats operator admin/MFA (or recovery:admin service
scope) checks at transaction admission and checks exact tenant, firing digest,
current policy fingerprint, schedule digest and canonical independent Evidence.
Proof requires the exact owner/event/effect snapshot, genuine writer quiescence,
quiescent effect/retention writers and the truthful succeeded/failed/unknown outcome. Current local
executor zero and elapsed time are insufficient. Incomplete/foreign or invalidated proof refuses custody release. The operator must independently establish
that original owner and every actual writer are terminated/fenced; the service
cannot manufacture this proof from its process-local executor.

No backup, verification, retention or original effect is replayed. The original
receipt remains in the reconciliation record. Proven terminal failure leaves the
firing diagnostic status `unknown`; it is never rewritten as success. Even after
valid reconciliation, health stays unqualified until a distinct new backup and
PASS isolated restore, both later than reconciliation and on the current policy,
satisfy the normal RPO/RTO/freshness gates. Invalidated/expired evidence remains a
blocker. Lost response is read-only receipt reconciliation using the same stable
key; conflicting keys/snapshots and late original settlement are refused.

The executor exposes two bounded terminal observations (event ID/error class,
not exception payload or effect success), distinguishing a terminal failed future
from unobserved actual effects. The demonstrated synthetic path is a settlement
write failure after real admitted work; that is not a diagnosis of the historical
production firing. Existing Recovery status/UI includes original identity,
operator disposition, Evidence/verification references and the next guarded
operator action; a reconcile receipt alone is not a qualified installation.

#### Installed application integration and explicit activation

The trusted application composition registers `RecoveryOperatorActions` and its
`RecoveryActionProvider` using the existing ActionProviderRegistry, ActionIntent
service and local ActionIntent worker principal. Registration is code availability
only: it creates no binding, Role grant, procedure definition, enabled feature or
Recovery1.3 write. A caller cannot load arbitrary code, supply a worker identity,
forge operator assurance or change the action through these routes.

An operator explicitly configures a scoped `recovery` / `canonical-recovery`
binding through the EXISTING `/api/action-providers/bindings` administration API.
Binding/enabling does not grant `recovery.scheduled-firing.reconcile` authority.
Current role/policy/security/procedure/provider admission and the existing worker
scope remain authoritative; a missing tenant worker, disabled binding or denied
procedure refuses. There is no fallback direct engine call or second queue.

The existing Recovery UI uses these scoped ordinary operator routes:

- GET `/api/recovery/scheduled-firings/{event_id}/reconciliation-preview` reads
  exact state/firing/policy/schedule digests, owner/effect identity and whether
  first1.3 activation is required.
- POST `/api/recovery/scheduled-firings/{event_id}/reconcile` accepts only
  `binding_id` plus the typed `RecoveryReconcileRequest`, including stable key,
  independent Evidence/verification references and explicit activation flag.
  The server derives actor/tenant/action and creates the existing durable
  ActionIntent, then uses the existing service worker's claim/execute boundary.
  An already attempted intent is returned without another execute.
- POST `/api/recovery/scheduled-firings/{event_id}/reconciliation-intents/{intent_id}/observe`
  binds the current operator/firing/action to that intent and calls the existing
  ActionIntent reconciliation with `retry_if_idempotent=false`. It never executes
  the engine or original backup effect to manufacture a missing receipt.

The request-scoped provider resolver reauthenticates through the existing HTTP
Identity boundary. Session rotation/revocation/expiry, MFA and membership are
current, not inferred from an identity ID or copied assurance. Revalidation does
not touch session/token timestamps or retire expired sessions inside Recovery's
shared transaction. Local-trusted assurance is retained only for an already
middleware-authenticated local request while the existing local-trusted mode is
still effective; membership/identity are reread. No credentials enter intent
parameters, UI, ordinary logs or persisted provider state. Outside an admitted
operator request the provider refuses, including generic effect execution.

The executable bootstrap sequence is the EXISTING qualified source-upgrade path,
not the old proposed host-code loader. A separate exact-tree upgrade may explicitly
set `require_recovery_qualification=false` only when current operator policy
approves that per-plan exception. It must still obtain a fresh protected backup,
PASS isolated restore, independent compatibility/preservation proof, complete drain,
current actor/MFA/plan CAS, guarded helper and postverification. Original firing,
UNKNOWN effect and aggregate Recovery health stay unchanged through installation.
It does not activate1.3 or authorize an API action. No new privileged host executor
or application import against production is introduced.

After the qualified source is installed, actual operator activation requires:

1. Independently inventory/fence every original owner incarnation, PID birth,
   future/callback/process group and effect/destination/retention writer. Zero local
   queue and elapsed time do not prove failure, never-started or quiescence.
2. Obtain current canonical independently verified `recovery-owner-disposition.v2`
   Evidence binding exact event/owner/effect, complete writer inventory/fence
   digests, actual custody/effect disposition, qualified activation and backup/
   isolated-restore proof. Absence of a file is not terminal effect proof.
3. Read a fresh preview, explicitly confirm the one-way1.3 boundary and submit
   only the immutable typed request through the operator route. The unchanged
   core validates current proof and state/policy/firing/schedule CAS in its shared
   transaction, retains the original receipt and records truthful UNKNOWN.
4. After any response loss, observe the SAME intent/key without replay. Independently
   verify its durable receipt and original effect identity. Obtain a distinct fresh
   backup and PASS isolated restore before claiming Recovery health/issue closure.

After any persisted1.3 write, an old1.2 writer refuses. There is no source-only
rollback; restoration requires the protected backup and its separately authorized
restore/preservation contract. These are source contracts, not evidence that any
production binding, activation or repair has occurred.

#### Unknown outcome and explicit codec activation

`recovery-owner-disposition.v2` separates custody release from original effect
outcome. Independent Evidence binds the full Recovery state digest, original
firing, policy, schedule, all-writer inventory/fence-proof digests, actual
quiescence of effect/retention writers and original-owner fencing. Known success
additionally requires the reserved scoped canonical result. Proven failure must
be based on the original operation, not absence. `unknown` is accepted only with
the same complete quiescence/fencing proof; the original receipt/effect remain
unchanged in history, current firing stays `unknown`, and no effect is replayed.
New independent backup/restore is eligible only after that release; qualification
remains blocked until both are genuinely fresh and PASS on current policy.
Evidence invalidation/expiration blocks qualification again. Local queue0,
elapsed time, an absent manifest or a post-purge missing file supplies no proof.

The1.2-to1.3 automatic migration has been removed. Loading1.2 or starting the
service never persists a normalization. Ordinary updates encode1.2 without the
new field, remaining readable/writable by the frozen5db1.2 codec. Only explicit
`activate_state_1_3=true` with the independently verified activation and
backup/restore proof may perform the first1.3 write, in the same transaction as
full-state/firing/policy/schedule CAS and reconciliation. Failed proof/CAS leaves
all documents at1.2. After activation the codec refuses downgrades with retained
records; frozen original1.2 load/update must refuse without mutation. This is a
one-way persisted boundary, not a configuration flag or startup migration.

#### Provider receipt loss and operator request custody

The provider uses canonical `ActionEvidence.evidence_type`, `reference` and
scalar metadata. The reference identifies the existing canonical disposition
Evidence; metadata binds event/owner/effect/verification/request digest. Stable
execution identity and observed timestamps derive from the single persisted
domain receipt. `readback` validates current actor, exact immutable request and
independent proof, then reconstructs that typed result without invoking the
engine, generating Evidence or executing any backup/restore/retention effect.
`prepare` exposes the same read-only observed result if one exists. Missing
receipt is **unknown execution**, never evidence of no effect or permission to
retry. Verification compares the complete canonical typed result, including
Evidence references, rather than accepting caller-altered Evidence.

The source now adds Recovery handling to the EXISTING ActionIntent reconcile
operation with `retry_if_idempotent=false`. It resolves the current registered
provider/binding, observes its `prepare` readback, verifies the exact typed
receipt and persists observation, verification and terminal status through the
canonical store. Missing domain receipt remains unknown. Explicit retry and
automatic repeated attempts are disallowed. No separate mutation route or
provider execution is used for reconciliation, and no Work Item success is
advanced by this custody-only action.

Current requester authority/security, provider/Definition, independent proof and
all canonical admission stores are rechecked; shared namespace revision guards
fence verification and terminal writes against intervening revocation. Observed
results are retained if subsequent verification/admission is denied. Configured
procedure admission is invoked for verification under the existing ActionIntent
context. Production must have its actual registered procedure/profile controls;
a standalone synthetic test configuration is not a production grant.

Core reconciliation and lost-result settlement were installed inactive through a
separately qualified source-only upgrade. The application/operator adapter added
here requires its own exact-head review, current CI and qualified source upgrade.
No source/fixture result proves production registration, binding, procedure
admission, all-writer fencing, first1.3 activation or original effect resolution.


### Operator integration qualification

`tests.test_recovery_operator_api` exercises the real HTTP identity middleware,
Recovery router, provider registry, ActionIntent worker and Recovery stores using
synthetic data. `tests.test_recovery_operator_postgres` repeats that composition
on an isolated PostgreSQL schema with single-connection pools, adds independent
late-writer and schedule-CAS connections, and checks concurrent same-key requests.
The distributed-backends CI job selects the PostgreSQL module explicitly;
missing isolated PostgreSQL is a skip, never PostgreSQL qualification.
Cancellation before domain commit preserves original state; lost-result readback
uses the immutable receipt without another effect. These fixtures do not prove
production writer termination, qualify an operator's evidence, or activate a
live state contract. Actual process restart/drain and installed UI acceptance
remain operator qualification, alongside exact-current-head CI and review.


### Manual HTTP writer custody and reconciliation CAS

Manual backup/verification HTTP requests use the same bounded RecoveryExecutor
as scheduled operations. The actual submitted future remains owned until its
worker is terminal, including after observer cancellation. Close/shutdown also
closes manual admission and waits for that work; a lost response is an unknown
observation, never permission for an automatic repeat. Current request identity,
credential rotation, tenant and operator assurance are revalidated at worker
entry and at Recovery/key/Evidence settlement. Manual settlement includes the
existing identity document in the shared transaction and returns its full payload
unchanged; its storage revision may advance, without session touch/normalization.

Reconciliation holds the executor's local quiescence admission barrier across
its complete canonical CAS. Active/queued manual or scheduled work refuses;
manual/scheduled submissions cannot begin behind that CAS. The barrier is not a
proof about another process, upgrade helper, direct trusted service caller,
callback, destination writer, or the original effect. Those remain explicit
independent writer-incarnation/fence/effect evidence obligations. This does not
claim every trusted synchronous Recovery helper is an executor task. Dry-run,
zero local futures, source installation and this local barrier cannot substitute
for the original all-writer proof or activate state1.3.

The bounded executor may serialize reconciliation behind already accepted manual
work. Queue custody remains visible; observers must not hold a fixture or
external writer barrier while awaiting that queued operation. Once work reaches
the CAS, a changed Recovery snapshot still refuses; queue admission is not a
reservation of an earlier snapshot or authority.

Manual retention compares the captured backup policy fingerprint with the
current policy under the existing Recovery/key/identity transaction before any
deletion. A changed policy refuses retention, preserving the already committed
manifest and encrypted bytes as diagnostic results. That conflict is not a
successful backup response or permission to replay the backup. Destination
deletion failures can have unknown external outcomes; the transaction does not
promise rollback of arbitrary destination side effects.


Operator observation preserves two identities: the HTTP requester remains the
current administrator/MFA and proof subject, while canonical ActionIntent
settlement uses the already registered service worker, just as initial execution
does. An administrator is not granted worker scopes. The requester/tenant/firing
binding is checked before delegation, and provider readback/settlement recheck
current human authority. The normal service-principal guard is unchanged.


### Final irreversible-write admission and provider custody

Recovery provider mutation is entered only from the actual ActionIntent executor
through an internal process-local context; no request can supply that context,
a worker identity, a guard callback or a successful admission flag. The existing
Recovery executor owns the entire mutation/admission worker, including its real
concurrent future during cancellation and ActionIntent timeout. Stop closes
admission and joins that future. Readback remains receipt-only and never calls
the mutation worker again.

Each observer owns one non-failing completion notification for the real worker
future. Worker exceptions are retrieved from the terminal concurrent future,
not copied into repeatedly cancelled asyncio proxies. After observing caller
cancellation, joining shields the existing ASGI cancellation scope so level
cancellation cannot spin or multiply callbacks/logged exceptions. The original
caller cancellation is propagated only after the worker has actually ended;
without cancellation, the actual worker result or failure is propagated.

For operator/provider first state1.3 persistence, the existing canonical record_mutate_many
transaction acquires the globally sorted shared admission and Recovery/key/
Evidence/Scheduler locks together. It pins/rechecks current HTTP authentication,
MFA/expiry/membership, worker identity and scope, binding and ActionDefinition,
Role/Definition authority, policy/security, current procedure discovery and
applicable durable procedure step. Admission rows are guard-only; only the
actual domain deltas are written. Checks run before and after the domain updater;
a final ActionIntent failure is not a substitute for preventing a denied domain
migration. A separate writer committed before these locks causes refusal;
a writer acquiring the same locks afterward is ordered after the domain commit.

Independent original-writer/effect proof, explicit activation and qualified
backup/restore/drain remain mandatory and are not produced by these guards.
Manual HTTP offload is retained source awaiting the explicit bounded follow-up
tracking decision under stabilization/#1489 provenance; it is not silently
accepted as extra scope or published while that tracking remains pending.

## Lossless StateStore preservation format

`state-storage-preservation@1.0` is an additive opaque storage format, separate
from semantic `BackupSnapshot@1.0`. It is not recorded in RecoveryState's backup
or verification collections and cannot advance RPO/RTO or Recovery health.
Semantic backup decoding accepts exactly format 1.0; a preservation header lacks
`documents` and is rejected by older strict semantic readers as well.

The raw snapshot includes both physical StateStore tables, legacy documents,
keyed collection markers and rows, and metadata. It preserves UTF-8 TEXT bytes
without interpreting payload JSON, and pins backend/schema/encoding. Records
are ordered by table and UTF-8 key bytes. Canonical ASCII JSON records encode
payload bytes with base64 and timestamps as big-endian IEEE754 binary64.
Unsigned 64-bit frame lengths, ordered SHA256 and exact counts bind the header,
rows and terminal footer. Unsupported types, schemas, versions, ordering,
nonfinite timestamps, duplicates, incomplete input or resource limits refuse.
The promise is logical storage equality, not database-file or heap-page equality.

PostgreSQL export owns one REPEATABLE READ READ ONLY transaction across both
tables and sizes before fetching payloads. At most 64 key/length entries are
paged; payload groups total at most 8 MiB, except one large row alone. SQLite
uses one explicit read transaction. The defensive parser accommodates up to one
million rows, 32 MiB values, 64 KiB keys, 2 GiB aggregate payload and 4 GiB framed
plaintext. These are format ceilings, not runtime grants. Effective deadline,
CPU, memory and disk admission must independently authorize an operation. An
active PostgreSQL callback cannot be silently promoted from READ COMMITTED;
raw export refuses before trying a nested pool checkout.

Raw import is same-backend only, into an independently isolated, non-running
empty target. The storage primitive takes a target write transaction and checks
that it contains no document rows and only its recognized schema seed. It
replaces that seed with exact exported metadata and commits only after complete
footer validation. Every error or cancellation rolls back that transaction.
The primitive does not authorize startup or prove deployment isolation; the
operator/action boundary must independently verify the actual target resource.
It is not exposed as an overwrite-live-store endpoint.

The encrypted container composes existing CryptoKeyService envelopes in 256 KiB
plaintext chunks, with snapshot/scope/index-bound contexts and a separately
authenticated manifest binding the header, ordered encrypted chunk hashes and
counts. Missing, foreign, duplicated, reordered or truncated content refuses.
A consumer must exhaust authentication before committing a target. The batch
key is resolved before opening the source snapshot, then rechecked and audited
after source cursor closure, before the terminal encrypted manifest can be
published. This avoids auditing through a read-only owning transaction or a
nested checkout of a one-connection pool. A raw snapshot preserves its original
rows; mandatory subsequent crypto audit appends are separately acknowledged and
must not be represented as zero source activity. Source and isolated target
connection custody must remain independent when decoding into a transaction.

These low-level primitives and codecs do not create authority, a trusted action
binding, an Artifact receipt or an installation approval. Governed capture must
still perform exact whole-store resource authorization/current identity/policy/
key admission, actual resource reservation, tracked executor custody, immutable
Artifact publication, independent verification and loss-response reconciliation.
No RecoveryState 1.3 activation or persisted reconciliation migration follows
from using this additive format.

Usage history is a distinct read-only projection. Bounded inspection uses a
consistent snapshot and never calls usage migration or replaces retained rows.
It preserves authorized WorkItem history even when usage cannot be completely
interpreted. `usage_diagnostics@1.0` reports incomplete/unavailable separately
from zero; incomplete totals are null. Unknown-scope corruption never supplies
foreign identifiers, row counts, digests or parser excerpts to an ordinary
WorkItem caller. Strict accounting reads/writes remain fail-closed. Existing
operator history renders the generic incomplete status; detailed storage
inspection requires independent whole-store authority.

### Governed opaque-preservation operations

`recovery.storage-preservation.capture` and
`recovery.storage-preservation.verify` are additive, single-attempt, high-impact
canonical ActionProvider operations. The application registers their trusted
implementation, **not** a binding, authority grant, deployment value or an
operation. No startup migration or RecoveryState1.3 activation occurs. Existing
RecoveryState1.2/1.3, scheduled firing/effect identity, semantic backup/restore,
RPO and health rules remain authoritative and unchanged.

The existing Recovery operator page previews the exact whole-StateStore Resource
and effective budget, requires high-impact confirmation, and exposes canonical
ActionIntent, receipt, verification, Artifact and Evidence references. Unknown
transport or missing domain receipt offers read-only observation, never another
capture. Ordinary usage history has only typed incomplete/null-total diagnostics;
operator storage reports contain bounded opaque key references, byte lengths,
digests and fixed error codes, never payload excerpts. Valid JSON by itself is
not a semantic domain validation. Successful storage operations consequently
produce WARN Evidence with `semantic_recovery_qualified=false` and do not advance
a Work Item or satisfy release/recovery qualification automatically.

Deployment administration must publish the following existing Configuration
Registry keys before availability can be asserted:

- `recovery.preservation.resource_id`: the explicitly authorized whole-StateStore
  DATABASE Resource. A requester cannot select another source or filesystem.
- `recovery.preservation.memory_bytes`, `disk_bytes`, `wall_seconds`: effective
  admitted budgets, with wall10..300 seconds intersecting the existing runtime
  allowance. These configuration values grant neither capacity nor authority.

An explicitly enabled `recovery-storage` / `canonical-state-preservation`
binding must target exactly that Resource and authorize the actual content and
database-reservation directories through its existing filesystem policy. Current
requester tenant, administrator/MFA session, action capability/role, Resource,
provider/binding/Definition, execution assignment/fence/lease, procedure/security,
quota and worker identity remain canonical admission requirements. The existing
mutation guard locks/rechecks their shared transaction read set immediately
around irreversible Artifact/Evidence publication; configuration, policy, key
and capacity Evidence are also read under their shared canonical locks. A
security recheck inside a shared transaction uses the same pure boundary
assessment as ordinary audited admission. It must not append a SecurityEvent
from inside the transaction callback. Ordinary ActionIntent admission continues
to record its security decision; both mutation and read-only receipt settlement
recompute the boundary decision under the final locks without nested writes.
A revocation committed before the final locked read prohibits publication. A
writer arriving after that read serializes behind the transaction. No serialized
caller parameter can install the internal worker context or lend worker scopes
to a human.

The independent capacity prerequisite uses existing canonical Evidence and
Verification, not a new registry or caller assertion. Evidence metadata contract
`recovery-storage-capacity.v1` binds `resource_id`, the SHA256 of canonical JSON
configuration pins `{name:[record_id,revision]}`, integer `memory_bytes`,
`disk_bytes`, `wall_seconds`, numeric `expires_at`, and absolute
`database_directory` / `database_reservation_directory`. It must also bind the
current `recovery_executor_owner_id`, `memory_enforcement=kernel-cgroup-aggregate`,
`memory_max_bytes` equal to the admitted memory budget and
`memory_swap_max_bytes=0`. A restarted runtime cannot adopt an old incarnation's
proof, and a larger global memory cap is not evidence of a smaller operation cap.
An independent VERIFIED
record must reference this current, valid, retained, same-tenant Evidence. Start
freshness is60seconds, expiry covers the operation wall budget; commit rechecks
expiry, validity and the original admitted pins. The directory mapping must
match the actual configured database and reservation device. Symlinked mappings
are refused. Evidence must describe **actual external enforcement** where
required: neither RLIMIT_AS nor cooperative row estimates establish a job cgroup.
No API call here provisions a cgroup, raises a quota, invents independent proof or
creates any production binding/configuration.

Admin/MFA-scoped storage status and preview expose
`recovery_executor_owner_id` from the **current** Recovery schedule-admission
instance, with `incarnation_provenance.source=current_runtime.recovery_schedule_admission`.
This is read-only process-incarnation provenance, not a capacity/health assertion
or a historical firing owner. A restart produces a different owner and invalidates
old capacity proof; no firing needs to run to discover it. The existing operator
surface renders the escaped reference and restart warning.

The existing assignment-bound Verification adapter additionally accepts exactly
`recovery-storage-capacity.v1`. It retains requester API authority, actual worker
attribution, governance, profile/lease/fence/current-owner and shared transaction
guards. The original capacity Evidence and its artifacts must remain requester-
produced, reviewable and independent of the actual worker. Configuration and
provider binding records join the append read set. The currently registered
trusted preservation provider, exact enabled DATABASE binding, published config
pins, active Project-associated DATABASE, runtime owner and original fresh
kernel/filesystem metadata are rechecked using the same capacity predicate as
execution. No caller assertion establishes real enforcement.

Project association does **not** grant an assignment access to that DATABASE.
The exact DATABASE must already be admitted in both canonical Work Item and
assignment resources, alongside the writable repository and lease. A repository-
only thread cannot borrow its repository permission for capacity review. When
absent, ordinary operator Work Item/resource and fresh execution/workspace
admission is a prerequisite; publication-recovery alone does not expand it.
No adapter fallback, Role grant or actor substitution is introduced. Verification
only records reviewed storage capacity; CAPTURE/VERIFY still require the existing
current admin/MFA operator and all actual execution controls. Response loss uses
exact execution/Work Item/review-digest readback for either supported method,
never automatic append retry. Ambiguous lookup stays unknown.

Before reading payloads, a consistent key/length-only snapshot computes exact
base64 row-frame sizes using a bounded PG server cursor. Reservation includes
explicit bounded growth headroom, encrypted-frame maxima, content overlap and
same-backend target/index/WAL allowance. The source capture is a distinct single
consistent snapshot; its emitted bytes must fit the reserved bound, or capture
refuses. Disk reservations use owned private allocated files on the actual
filesystems and remain held until real I/O joins. Configured charge covers both
reservation and writes, with current free-space checks. This is cooperative
operation accounting, not a hard quota or a promise that the complete backup
workflow has a fixed heap footprint. Changed source size, unavailable filesystem,
insufficient resource proof or exhausted bounds produces refusal, not truncation.

Capture reserves a restricted `deny_model_context` Artifact with UNKNOWN custody,
streams encrypted content through existing ArtifactContent/KMS, authenticates the
entire terminal manifest and checks content length/digest. A completed encrypted
object reference is retained as protected pending metadata before final
admission. Pending metadata does not make content available or claim integrity.
A crash between a filesystem write and that custody append remains UNKNOWN;
there is no atomic filesystem/database guarantee, orphan adoption or replay.
Only the shared Artifact/Evidence transaction attaches the immutable available
pointer and appends its idempotent storage-integrity receipt. Repeated canonical
requests/readbacks preserve the same intent/Artifact/Evidence; loss after commit
settles through existing read-only ActionIntent reconciliation, not engine
execution. Later authority denial can retain an observation without successful
settlement. Distinct verification attempts remain canonical and auditable.

Verification decrypts the original immutable capture and writes only an owned
new SQLite file or newly generated PG schema through the same raw restore
contract. No application starts on it, no side effects run, and no live target or
existing schema can be adopted. The isolated transaction exhausts authenticated
input before committing; the resulting full raw stream digest must match before
owned cleanup and Evidence publication. Cleanup failure prevents successful
verification and retains unknown operation custody. PG verification uses its
own connections and never steals the source callback/pool1 transaction.

Cancellation signals the existing cooperative StorageOperationBudget and joins
the actual bounded Recovery executor future. It cannot abandon a writer or make
drain report zero before the thread terminates. Content and target writes must
finish/refuse before reservations release. Timeouts do not permit automatic
retry. This API performs no live import, record repair, semantic normalization,
original firing disposition or accounting reinterpretation.

The versioned storage format and protected Artifact are separate from legacy
BackupSnapshot1.0. Existing semantic readers must refuse this format; storage
integrity/isolated physical restore is not a semantic RestoreVerification PASS.
The operator bootstrap must independently retain an authenticated complete
backup and isolated restore proof accepted by the reviewed deployment contract,
plus drain, exact-source/CAS, preservation and postverification gates. This
source integration neither waives those gates nor authorizes installation,
RecoveryState1.3 activation, old-writer handover or binary rollback after an
irreversible contract transition. Full-current-shape encrypted qualification is
separate from focused small fixtures and must use real admitted aggregate
memory/disk/CPU/process/time enforcement with all clients/servers reaped.


#### Review corrections: scoped metrics and independent preservation oracle

History storage inspection completeness is separate from completeness of each
visible joined execution metric. Missing `total_tokens` or `cost_usd` on an
otherwise valid authorized record makes only that role/metric total unknown;
observed zero remains zero and a known record count remains known. Team/work-item,
tenant and visible-execution joins happen before metric completeness is derived.
A valid foreign or unrelated record cannot alter those scoped totals. Typed
`storage_complete` and per-role `metric_completeness` explain partial observations
without disclosing foreign record identities. Unknown-scope malformed storage
still produces generic incomplete/null totals, with no immutable accounting write.

Chunk and terminal-manifest envelope schema failures use the same fixed
`invalid_encrypted_envelope` refusal. The decoder raises outside the Pydantic
handler, without attaching its untrusted input exception as context; public
operations retain only sanitized errors and no successful verification receipt.

Isolated raw restore batches at most256 rows or4MiB of UTF8 key/value bytes per
`executemany` call (one permitted large row is flushed by itself). PostgreSQL
uses its existing same-connection pipelined executemany, retaining one exclusive
atomic transaction. Budget/cancellation checks bracket batches and remain per
row. Earlier inserts are never a partial result: authenticated footer/EOF,
schema/count/order/digest and final budget checks still precede commit; any
failure rolls back all batches. No source write or second execution engine is
introduced. Independent tests compare actual SQL TEXT and timestamps (including
metadata) and the literal seeded full-scale rows; matching encoder digests alone
are not preservation proof. Full-scale phase timing/actual admitted resource
qualification remains required before any performance claim.


#### Capture-sized admission and retained encryption consumers

Storage report1.0 includes a strict inventory of the authenticated capture:
row count, payload/key bytes, maximum row bytes and framed bytes. VERIFY obtains
it from the capture's canonical, private-idempotency Evidence identity, checks
its producer/intent/resource/request/Artifact binding, and reserves for that
capture even if the current live database has shrunk. Captures without that
inventory/referral refuse; the current live database is never substituted.
Before any new isolated target exists, the captured maximum row must fit the
admitted memory estimate and actual disk reservations. Capture growth is limited
by row/payload/max-row/framing ceilings passed into the source's consistent SQL
metadata preflight **before payload selection**, not just a final byte counter.
Both assessment and restore enforce the same effective limits. Each bounded
restore batch also checks actual target relation/page growth on its owning
transaction and rolls back on excess. These are cooperative growth checks;
filesystem reservations and a real aggregate job memory bound remain necessary.
The operation makes no hard disk-quota or whole-backup heap guarantee.

Before encryption, CAPTURE reserves the exact `preservation_key_id` and integer
`preservation_key_version` on its existing restricted Artifact. This reservation
locks the Artifact and CryptoKey documents together with the normal admission
read set and checks the selected current key before appending. KMS encryption
pins this exact version and final publication rechecks current key/configuration.
CryptoKey retirement reads preservation Artifacts in the same sorted shared
transaction as Recovery/conversation consumers. An earlier retained reservation
therefore prevents revocation; earlier rotation/revocation prevents capture
publication. No semantic BackupManifest or additional consumer store is created.
UNKNOWN reservation, pending content, invalidation, supersession and expiry do
not retire the dependency. Only the existing physical content-delete procedure's
verified TOMBSTONED pointer, with no pending content, releases it. Missing key
metadata on a retained preservation Artifact makes impact unavailable rather
than guessing that revocation is safe. Tenant and exact key/version filtering
remain enforced, and existing key impact UI renders the canonical consumer.

**Deployment boundary:** these new Artifacts must not be created while an older
key-retirement implementation remains an authorized writer. Registration alone
does not enable this action: an operator must qualify all key-writer binaries,
drain old writers, retain backup/isolated-restore proof and enable the existing
binding/configuration only after that check. The existing crypto-state1.0 schema
does not fence an old binary; no automatic old-writer refusal or safe source-only
rollback after the first retained preservation consumer is claimed. Returning
to an older implementation requires a separately qualified compatibility plan
which preserves/protects all retained and UNKNOWN key dependencies. Source
publication does not authorize production binding/activation or rollback.

SQLite's real progress-handler SQLITE_INTERRUPT is normalized to the existing
StorageOperationBudget TimeoutError only when that budget actually cancelled or
expired, after the transaction context has rolled back. Unrelated SQLite errors
retain their original refusal; cancellation never means partial restore success.


Envelope batch decryption accepts the selected ACTIVE or DECRYPT_ONLY version
under an ACTIVE key, matching individual KMS decryption of retained envelopes.
The same rule is checked again at the terminal audit under the canonical key
transaction: concurrent key/version revocation or changed backend reference
refuses completion. Rotation alone can retain decrypt-only access. New batch
encryption still requires the current ACTIVE version at entry and completion;
retained decrypt-only material never authorizes new encryption. Scope, context,
AAD authentication and closed-batch lifetime checks are unchanged.


VERIFY reuses only the canonical CAPTURE producer's immutable assessment rather
than decrypting the same content twice. The private idempotent receipt identity,
actual producer/tenant/resource/action/original request, Artifact pointer digest
and byte length, frozen key id/version and strict report inventory must agree.
Missing old receipt fields or a public Evidence clone refuse before any target
creation. CAPTURE atomically publishes those content bindings with its report
and Artifact pointer; this is additive unreleased preservation metadata, not a
semantic Recovery backup or normalization of original records.

Reuse does not establish verification by itself. The actual encrypted stream
must still be fully decrypted/authenticated, including KMS final current-version
checks, manifest, digest/length, row bounds, footer and EOF inside the isolated
restore transaction. Every restored physical target row is independently read
back through the bounded raw exporter and hashed against the bound capture
report. The isolated target is cleaned before successful Evidence publication.
The final shared transaction also requires the exact capture receipt to remain
unchanged and VALID along with Artifact/current authority/policy/key/capacity.
Late invalidation refuses publication. No target authentication, raw hash,
cancellation, cleanup, independent qualification SQL oracle or semantic-health
gate is skipped. Per-operation budgets, format and dataset limits are unchanged.


### Native same-thread read-only Resource context (1530 follow-up)

Ordinary scoped input uses the versioned alias
`POST /api/threads/{thread_id}/turns/resource-context-v1` of the existing TurnService.
`read_only_resource_ids` is a unique bounded tuple (at most eight nonrepository
IDs), default empty; `resource_context_work_item_ref` names the canonical Work
Item association. An old server returns404 for this path before user input.
The legacy turn endpoint refuses an explicit nonempty selector, rather than
claiming an unsupported field was admitted. The authenticated API actor, never
an actor field in input, supplies original requester provenance. Thread settings
can inspect/preserve that context; selecting/clearing it requires versioned turn
admission, not an unguarded settings update.

The frozen `ReadOnlyResourceContext@1.0` follows the original input through
settings, queued/preflight/native-start/resume snapshots, bootstrap, workspace,
assignment, active session and renewal. Empty context is omitted on serialization,
keeping old-empty snapshots equivalent; a nonempty future context version refuses.
Older retained inputs are not upgraded from today's settings. Current owner/
recipient, Project/tenant, active Resource, pinned profile and original requester's
actual `resource.read` authority must all hold. Project association and internal
control actors cannot grant this access. The exact associated Work Item is retained
in the immutable context. This genuinely admitted context is a second canonical
WorkItem/Resource association, independently of the imported issue's flat
`resource_ids`. A bare selector or Project association alone is insufficient.
Existing per-item reconciliation may instead refresh the flat resource list;
that fallback is not mandatory for the versioned context path.

Canonical workspace resource modes classify context as READ and repository as
WRITE; generic context is not a repository member, credential, network permission
or filesystem/data mount. Existing lease conflicts and resource/disk quotas still
apply. Same execution reuse requires identical provenance/modes. Session/proxy
validation rechecks actual assignment/workspace/lease and current authorization;
new work cannot silently change a live assignment. Scope change first refuses,
then requires supported terminal custody/fencing and fresh SAME-thread bootstrap.
Native procedure discovery recognizes this existing READ context only when the
frozen assignment/workspace context, original requester and every live READ lease
mode match. Unbound nonrepository targets remain unsupported native tools.
This preserves isolated preparation; it grants no database credential, mount,
network or host mutation capability. Selected operational procedures still require
their canonical action boundary, even when their targets are READ context.
Binding replacement pins the previous assignment and retired fence in one shared
canonical transaction, and a failed CAS cancels/releases only the new candidate.
Renewal retains the previous context and limits, not newer settings.

Nonempty contexts use code-owned `thread-turn/1.1` or `thread-bootstrap/1.1`
execution contracts; old-empty requests keep1.0. No Profile/Role/Definition grant,
new executor, token or adoption endpoint is added. The installed-source capability
and supported-worker contract must be verified before sending the versioned input.
The first persisted nonempty context (including settings or queued snapshots) is
a compatibility boundary: old application readers/writers may omit unknown fields.
There is **no** claim that the old binary can safely roll back after that write;
all old application/worker writers must be drained/fenced under the reviewed
installation contract before enabling this path. Source publication does not
activate it, manufacture a worker's independence or qualify host capacity.

The capacity Verification adapter can recognize this genuinely admitted READ
context as an exact Work Item/Resource READ-lease association. The legacy flat
Work Item resource membership path remains supported independently. Current original-record
visibility/governance, current requester API read/write authority on that DATABASE,
actual authenticated verifier identity, same-tenant Project binding, exact config,
runtime owner and independent capacity Evidence remain separately mandatory.
The production app/PG resource measurements and enforcement are operator facts;
source/test fixture metadata is never substitute capacity proof.


For an existing imported issue, the operator first reads exact canonical source
identity, owner/handoff and active Project Resource bindings, then uses ordinary
`POST /api/work-items/{ref}/reconcile`. This operation rereads the authoritative
source and can reconcile ownership/labels/relations as well as Resource scope;
it is not a membership-only CAS. Pin source/custody before it and read them back
immediately afterwards. No current assignment scope changes as a side effect.
For reviewer ownership, ordinary permitted Developer-to-current-reviewer-profile
handoff and genuine recipient ACK are separate. An awaiting handoff does not
satisfy the admission owner check, nor does `quinn` identify the VP profile.
A repository-only reviewer can ACK first, followed by supported terminal custody,
fencing and fresh SAME-thread READ-context admission once the current owner/Project/READ admission conditions hold.
The composed synthetic regression uses the actual GitHub adapter/projector,
per-item reconcile, canonical WorkItem handoff/ACK, worker principal/authority,
and native admission. Only GitHub/native I/O and dispatch delivery are synthetic.

Legacy `ThreadRunSettings` and `QueuedTurn` readers default to ignoring unknown
fields. The regression explicitly demonstrates that their old field sets can
drop this context. A versioned input route, strict new context schema and 1.1
worker contract do not make those old persisted readers safe. First nonempty
input/settings/snapshot persistence therefore requires the declared coordinated
old-writer drain/fence and is an unsafe old-binary rollback boundary.

Qualification for this path must compose the versioned public input with the
real TurnService, retained queue/preflight and fresh canonical bootstrap; a
prebuilt database assignment or mocked TurnService alone is insufficient. The
requester, internal allocation controller and independently authenticated worker
remain distinct. Pending handoff refuses; genuine recipient ACK and terminal
repo-only binding retirement precede fresh admission. A separate capacity test
must append the original Evidence/Artifact Verification while the database is
absent from the flat Work Item list, and repeat current Project binding, READ
authority, owner, context, lease and fence checks at the shared append transaction.
These are source qualification requirements, not assertions of live admission
or capacity verification.


#### Authenticated retained steering and PostgreSQL setting refusal

Both ordinary HTTP queue-steering endpoints forward only the authenticated
request actor through TurnService to native admission. A nonempty retained
Resource context requires the same current requester before dequeue, steering
rate accounting, handoff or interruption; native admission repeats the check
and its existing authority/lease/fence validation. Stored requester/profile
fields and HTTP body actor fields cannot authenticate a caller. No-actor direct
service calls remain denied for nonempty context. Explicit old null context
remains null and never inherits newer thread settings.

Storage-preservation inventory and reservation mapping both read the actual
PostgreSQL `data_directory`. Insufficient setting-read privilege refuses with
`preservation_database_setting_read_denied`; the admin preview returns409 with
fixed operator guidance and `capacity_verified=false`. Driver errors, DSNs and
host paths are excluded. The existing Recovery UI displays that fixed guidance
as text. Other database errors are not reclassified. The operator must review
the database identity's setting-read permission through existing database
administration; this source change grants no role (including
`pg_read_all_settings`), uses no privileged fallback and fabricates no directory.
No capacity or preservation acceptance follows from a successful setting read.

At trusted application startup, the artifact backend pins the resolved durable
data directory before appending `artifact-content`. This supports an installed
checkout whose operator-controlled `data` link points to persistent runtime
storage. Retargeting that deployment link cannot redirect the running backend.
The content leaf and descendants are not resolved away: preservation still
rejects symlinks there and enforces the existing binding's real filesystem roots.
This changes neither stored content locators nor capacity, authority, or unknown
outcome rules; it performs no data move or repair.

Recovery's irreversible-write guard holds the existing secret-state namespace
lock and pins its complete metadata outside the use-audit list. Secret creation,
rotation, revocation, ACL and expiry changes still invalidate admission; normal
secret-use audit appends remain recorded and do not represent a grant change.
Other guarded namespace revisions, current actor/credential checks, lease/fence,
procedure and final transactional guards remain enforced. Repeated local-trusted
identity bootstrap and internal service bootstrap read an already complete state
without rewriting it; missing principals or memberships still initialize through
the existing atomic updater, and existing membership roles are preserved. This
neither adds membership authority nor suppresses audit records.
