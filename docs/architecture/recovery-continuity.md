# Production recovery, backup and continuity

## Status

Milestone 11 recovery contract. A backup is not considered recovery evidence until
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

Recovery's own manifest state is excluded from the snapshot to prevent recursive
backup growth.

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

The snapshot also freezes the complete canonical key manifest. Restore
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
source recovery-restore-verification. PASS Evidence is consumable by the M11
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

#168 owns broader application/worker/extension version-skew and migration
orchestration. Recovery records exact schema/policy metadata so those checks have
a deterministic input instead of guessing from backup age.

## API and UI

/api/recovery provides policy configuration, backup creation, restore
verification and recovery health/history. Human mutations require administrator
authority + MFA; service callers require recovery:admin.

Issues #121/#125 own UI presentation of backup age, last verified restore,
RPO/RTO status, destination/key/audit dependencies and guarded drill/restore
operations. #124 owns operator runbooks.
