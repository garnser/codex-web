# Encryption at rest and key management

## Status

Canonical encryption-at-rest and key-management foundation.

This architecture deliberately separates **secrets/credentials** from **cryptographic encryption keys**. SecretBroker remains the boundary for provider credentials. CryptoKeyService owns key metadata, versioning, scope, rotation and encryption/decryption behavior. Raw key material lives only in a KeyBackend.

## Cryptographic model

Codex-web uses envelope encryption.

For each encrypted value:

1. resolve the active tenant-scoped key-encryption key (KEK) version;
2. generate a fresh random 256-bit data-encryption key (DEK);
3. encrypt the plaintext with AES-256-GCM using the DEK;
4. wrap the DEK with AES-256-GCM using the KEK;
5. persist only ciphertext, wrapped DEK, nonces, key ID/version and context digest.

Raw KEK/DEK material is never placed in SQLite, JSON mirrors, audit events, or ordinary API output.

The first backend is an owner-only local-file key backend. The service contract is backend-neutral so KMS/HSM implementations can supply the same create/get/delete/health behavior without changing domain encryption code.

The Python `cryptography` package supplies authenticated encryption primitives; codex-web does not implement cryptography itself.

## Scope and AAD

Every key is scoped to organization + workspace and may be narrowed to project and/or resource.

Every ciphertext is bound through authenticated additional data (AAD) to:

- organization;
- workspace;
- object type;
- object ID;
- optional project;
- optional resource;
- key ID/version for DEK wrapping.

Changing any of those values causes authentication failure. This prevents a valid ciphertext or wrapped DEK from being substituted into another tenant, workspace, project, resource, or object identity.

## Classification policy

The code-owned baseline is:

- public/internal: encryption may be deployment/domain policy;
- confidential/restricted/secret: encryption at rest is required.

A stored mutable definition or ordinary configuration must not be able to lower this baseline. Domain integrations may make lower classifications stricter.

## Key lifecycle and rotation

A key has a stable ID with immutable numbered versions.

Rotation:

- creates a new backend key version;
- marks the previous version `decrypt_only`;
- makes the new version active for encryption;
- does **not** rewrite all data atomically.

Old ciphertext remains decryptable while its version is `decrypt_only`. `reencrypt` decrypts an old envelope and creates a new envelope using the current version, enabling online/batched migration with retryable progress.

A revoked key/version fails closed for decryption. Missing/unhealthy backends also fail explicitly; there is no plaintext fallback.

## Backup and restore

Backups containing encrypted data must carry a key manifest of required stable key IDs and versions, never key material.

Before restore, `validate_manifest` verifies that:

- required key metadata exists in the tenant/workspace;
- every required version exists;
- the backend still has each referenced version;
- the version is not revoked.

Restore tooling must stop when validation fails. Copying the application database without its external key backend is intentionally insufficient to decrypt protected data.

## Audit

Audit events contain key reference/version, actor, tenant/workspace, operation/outcome, object identity and reason code only.

They never retain plaintext, ciphertext, raw keys, wrapped DEKs, nonces, provider credentials, or arbitrary user content.

## API

Key administration:

```text
GET  /api/crypto/keys
POST /api/crypto/keys
GET  /api/crypto/keys/{key_id}
POST /api/crypto/keys/{key_id}/rotate
POST /api/crypto/keys/{key_id}/revoke
POST /api/crypto/keys/{key_id}/versions/{version}/revoke
GET  /api/crypto/backend-health
GET  /api/crypto/manifest
POST /api/crypto/manifest/validate
GET  /api/crypto/events
```

Raw key material and generic plaintext encrypt/decrypt endpoints are intentionally absent from the administration API. Product domains consume CryptoKeyService in-process.

Administrative metadata and mutations require tenant owner/admin or a service principal with `crypto:admin`.

## Adoption path

This foundation does not pretend existing plaintext domains became encrypted merely because a crypto API exists. Follow-up domain slices should:

1. register/select the correct scoped key;
2. require encryption for governed `confidential`+ records;
3. replace sensitive stored fields/blobs with EncryptedEnvelope;
4. keep searchable/indexable non-sensitive metadata separate;
5. add re-encryption/backfill jobs with resumable progress;
6. expose affected-data/re-encryption progress in the platform administration UI;
7. add each domain's key manifest requirements to the canonical backup/restore flow.

Likely early adopters are Executive memory/knowledge, Decisions, selected integration payloads, and artifact/evidence content-bearing fields.

## UI impact

The platform administration UI and operator workspaces should expose key IDs, purpose, scope, backend type/health, versions, active/decrypt-only/revoked state, creation/rotation/revocation timestamps, manifest/restore validation, and guarded rotation/revocation actions.

Raw key material must never be rendered.
