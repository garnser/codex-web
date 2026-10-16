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

Restore verification authenticates/decrypts the envelope, recomputes state
checksum/count, checks exact StateStore schema compatibility, validates key
references and verifies the backed-up autonomy-audit root against the root
recorded in the manifest.

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
