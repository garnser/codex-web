# Secrets and credential broker

Raw credentials are not configuration, policy, role definitions, work-item state, or model context. Codex-web represents persisted credentials by stable secret references and resolves material only at the provider/execution boundary.

## Canonical metadata vs secret material

`SecretReference` is canonical metadata stored in SQLite. It contains tenant scope, backend, purpose/provider metadata, ownership, use/reveal ACLs, expiration, rotation and revocation state. It never contains the credential value.

The initial local backend stores only opaque secret bytes beneath the private `data/secrets` directory with owner-only filesystem permissions. It deliberately does not duplicate metadata. Encryption-at-rest/key versioning is owned by #167; the local backend is structured so it can be replaced by Vault/KMS/cloud-secret-manager backends without changing callers.

Configuration values use `secret_id` references. Definitions and policy must also refer to secrets rather than embedding credentials.

## Use vs reveal

The broker separates three operations:

- metadata inspection;
- **use**, which passes the value directly into a bounded provider callback;
- **reveal**, which is separately authorized and is not exposed by the normal HTTP administration API.

A service principal needs both canonical tenant membership and `secret:use` scope plus explicit per-secret use permission. Human/service tenant scope is checked before material resolution.

Provider results are checked for accidental credential propagation. Supported strings/containers are scrubbed; an unsafely structured result that still contains the credential is rejected.

## Lifecycle

Creation stores material first, then canonical metadata; a metadata failure removes newly written material. Rotation updates the same stable reference and increments rotation metadata. Revocation makes future use/reveal fail deterministically without requiring role/config edits.

Expired or revoked references remain inspectable for audit but cannot be consumed.

## Audit

Create, use, reveal, rotate, revoke, denial and provider failure paths append metadata-only audit events with:

- secret reference ID;
- organization/workspace;
- actor identity and principal kind;
- operation and outcome;
- bounded context such as provider connection/project/action.

Audit records never include secret values.

## Existing integration migration

Bot connection models now support `*_secret_id` fields for bot tokens, Slack app tokens, signing secrets and webhook secrets. Authenticated bot connection saves move raw credential input into the broker and persist the reference instead. Bot runtime socket setup, polling, outbound delivery, channel discovery and webhook verification resolve those references at the point of use.

Legacy raw fields remain readable only as a compatibility path for existing installations and direct migration tooling. Newly authenticated saves prefer broker references. Deployment environment variables remain deployment-secret compatibility inputs rather than mutable database configuration.

## Browser/API boundary

The secrets administration API supports metadata listing, create, rotate, revoke and audit. Raw values are accepted on create/rotate requests but never returned. There is intentionally no ordinary reveal endpoint.

Sensitive secret administration requires canonical identity administration authority plus step-up/MFA-equivalent assurance. In local-trusted self-hosted mode the deliberate local administrator compatibility identity satisfies that assurance; enforced deployments should use a real authentication adapter.
