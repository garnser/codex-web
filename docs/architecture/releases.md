# Canonical release promotion and software supply-chain gates

## Status

Milestone 11 production-release contract. Releases are canonical records that bind
one immutable build artifact/digest to its source revision, SBOM, provenance,
signature state, policy revision, evidence, approvals, promotion targets and
known-good rollback artifact.

Release eligibility is deterministic. Models may summarize release notes but
cannot decide whether a release is eligible for promotion.

## Build once, promote immutable bytes

A Release freezes:

- canonical build/package Artifact ID and SHA-256 digest;
- source revision and build identity;
- SBOM Artifact reference;
- provenance Evidence reference;
- optional signature and signing credential reference;
- release-policy snapshot and fingerprint;
- test/CI/security/license/policy Evidence references;
- state/schema migration-compatibility Evidence when applicable;
- known-good rollback Release;
- structured release-notes/change-set Artifact where supplied.

Every Promotion copies the Release artifact ID/digest. Gate evaluation re-reads
the canonical Artifact and fails if its active digest differs. Production never
rebuilds from a branch or mutable source reference.

## Signing

Signing is policy controlled. The ReleaseArtifactSigner contract receives only
an artifact digest and a credential reference; secret key material stays behind
the signing implementation. The Release stores the signature, algorithm and key
reference, never signing key material.

When signing is required, an unsigned release, unavailable verifier, digest
mismatch or failed signature deterministically blocks promotion. A successful
signature check is also represented as canonical ARTIFACT_VERIFICATION Evidence.

The default local policy does not require signing because the base installation
does not configure a signing backend. Production policies can require it and
then fail closed until a signer is configured.

## Promotion gates

Promotion evaluates the exact Release policy snapshot. Gate inputs can include:

- required Evidence types such as tests, CI and security scans;
- provider-specific Evidence requirements;
- freshness limits;
- independent Verification;
- SBOM presence and lifecycle;
- provenance Evidence bound to the immutable release artifact;
- signature verification;
- migration compatibility Evidence;
- target Resource identity/risk;
- known-good rollback Release for production/high-risk targets.

The resulting gate record contains only deterministic blockers and matching
Evidence IDs.

## Canonical approvals and ActionIntent

A passing promotion gate can create a canonical ApprovalRequest bound to:

- operation release.promote;
- Promotion ID;
- Release version;
- exact artifact digest;
- target environment/deployment Resource;
- exact matching Evidence references;
- release-policy version.

The approval must be approved before queueing. Queueing consumes that exact
approval target and then creates a normal ActionIntent. Release code never calls
an ActionProvider directly.

The ActionIntent request contains the immutable artifact/digest, source revision,
SBOM/provenance references, rollout plan, release-policy fingerprint and
pre-recorded rollback artifact. Existing Role authority, trust-boundary,
credential, worker lease, provider idempotency and verification checks remain
authoritative.

## Staged rollout

Promotion records support all-at-once, canary and percentage rollout plans. The
provider receives the rollout plan through ActionRequest parameters and may
reject unsupported semantics during its normal prepare/execution contract.

Observation time and rollback-on-verification-failure are explicit release data,
not inferred by a model.

## Rollback

High-risk/production promotion can require a known-good rollback Release before
approval. Rollback creates another canonical ActionIntent targeting the
pre-recorded immutable rollback artifact/digest. It never rebuilds an older
branch/revision.

State-changing releases can require explicit migration-compatibility Evidence.
This makes forward/rollback schema constraints visible before promotion instead
of discovering them during emergency rollback.

## API

The authenticated /api/releases surface provides Release creation/list/read,
Promotion creation, gate evaluation, ApprovalRequest creation, deployment
queueing, status synchronization and rollback queueing. Mutations require
administrator authority and MFA for human users or release:admin service scope.

## UI

Issues #121/#125/#127 own presentation. The Autonomy Control Center and
Operations views should expose immutable artifact identity, SBOM/provenance,
signature status, gate blockers, ApprovalRequest state, rollout progress,
ActionIntent state, verification Evidence, blast radius and rollback target
without duplicating release eligibility logic in the browser.
