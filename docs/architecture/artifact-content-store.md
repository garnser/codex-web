# Artifact content storage boundary

Codex-web keeps canonical Artifact, Evidence, and Verification metadata separate from potentially large artifact bytes.

## Ownership

The canonical `Artifact` record remains the authority for identity, provenance, lifecycle, governance linkage, digest, and relationships. Managed byte content is replaceable infrastructure behind the `ArtifactContentStore` contract.

An Artifact may have no managed content at all. Git commits, pull requests, deployments, and provider-native records can remain reference-only artifacts. When managed content exists, the Artifact stores only a scoped backend pointer with size, media type, optional encryption-key reference, state, and verification timestamp. Provider credentials, bucket credentials, signed URLs, and raw keys are never stored on the Artifact.

Changing storage backend never changes the Artifact ID.

## Contract

`ArtifactContentStore` exposes provider-neutral capabilities for:

- streaming upload;
- bounded streaming reads and range reads;
- head metadata;
- SHA-256 verification;
- tombstone/deletion;
- backend health and capability inspection.

Every operation requires an explicit organization/workspace scope in addition to the backend locator. A locator alone is never authorization and cannot be used to cross a tenant boundary.

The local implementation uses content-addressed SHA-256 objects under hashed organization/workspace prefixes. The S3-compatible implementation follows the same contract and receives an already credential-bound client; it does not resolve or persist credentials itself. The same adapter shape can be used for MinIO and extended for native S3, Azure Blob, or GCS implementations.

## Selection and configuration

`artifact.content.backend` is a typed canonical configuration key. It may be set at deployment, organization, workspace, or project scope. Its value is only a registered backend ID and grants no authority.

The reference deployment registers the local filesystem backend as `local`. Additional backends are registered through composition/extension lifecycle rather than referenced directly from Artifact/Evidence domain code.

## Integrity and evidence

The canonical Artifact digest is SHA-256. Upload fails closed when bytes do not match an existing canonical digest. If an Artifact had no digest before managed bytes were attached, the upload digest becomes the canonical digest.

Content-backed artifacts are verified against their canonical digest when they participate in an evidence gate. Missing, tombstoned, truncated, or corrupted content cannot satisfy evidence requirements even when the Evidence metadata itself is still valid.

Migration copies a verified stream into the target backend, verifies the same digest, updates only the backend pointer, and can tombstone the previous copy. Artifact IDs and Evidence relationships remain unchanged.

## Governance and retention

Managed content is tombstoned before codex-web records an Artifact governance redaction/delete/anonymization receipt or retention expiry. If storage deletion fails, the governance/retention operation fails rather than falsely claiming that managed bytes were removed.

External Git/provider content remains outside this handler. Codex-web never claims to have deleted external provider content merely because an Artifact reference was redacted.

Encryption material is represented only by an optional key reference on the content pointer. Backends must receive only the key/secret capability needed for the specific operation; raw key material must not be persisted in Artifact metadata.

## Backup and restore

The canonical SQLite/database backup is not sufficient for managed artifact bytes.

A complete backup consists of:

1. the canonical codex-web database/state;
2. the configured artifact-content backend data;
3. encryption/key material or external key references required to decrypt that content.

For the local reference backend, back up the configured `data/artifact-content` tree together with the canonical database. Preserve file metadata/tombstones and restore it under the same backend ID.

For object stores, use the provider's durable backup/versioning/replication controls and retain the object namespace referenced by canonical Artifact pointers. Do not rewrite Artifact IDs during restore.

After restore, verify content-backed Artifacts against their canonical SHA-256 values. A missing/corrupt object is a visible integrity failure and must not be treated as valid evidence. Because the canonical Artifact record is authoritative, orphaned backend objects may be reconciled safely, but a backend object must never recreate or authorize an Artifact record on its own.

## UI impact

Shared Artifact/Evidence UI should expose availability, size, digest, media type, retention/classification, backend ID, verification status, and backend health where useful. It must not expose credentials, raw signed URLs, encryption material, or backend-internal authorization tokens.

The shared UI work remains part of the provider-neutral operator surfaces tracked by #125 and #127.
