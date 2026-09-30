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
The explicit `1.0 → 1.1` migration preserves all existing policy, backup,
verification and schedule data. Unknown future versions fail closed. Legacy
ownership is adopted only from canonical key metadata, never request text. Once
`1.1` has been persisted, application rollback requires an engine supporting
that contract; older binaries must not be assumed able to read the new fields.
Policy rollback does not downgrade this state contract.

Policy history survives isolated restore without recursively including old
backup/verification manifests. New backup key requirements come from the exact
frozen key document and exclude already revoked versions; retiring an unused
reference therefore does not make every future backup impossible. Revocation
racing with backup creation still fails the atomic reference-creation check.
Legacy policy adoption uses the transaction's locked key document rather than
opening a nested StateStore connection.
