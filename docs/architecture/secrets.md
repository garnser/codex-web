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

## Project Secrets and consumer context

`/projects/{project_id}/secrets` (including the `/codex` mount) exposes the
existing broker lifecycle in main page content. Secret references remain owned
by their organization/workspace and governed by their existing identity ACLs.
Project selection does not create a new secret store, ownership field or grant.
The view labels shared ownership, current-actor use permission, the absence of a
reveal API, status, provider/purpose metadata and rotation/audit provenance.
Creation produces a workspace reference; binding and unbinding use the existing
TaskSource and typed configuration workflows. Those selectors link back to the
Project Secrets page. Independent restore or hard deletion of a revoked reference
is not supported; operators create a replacement and update its consumer binding.

The secret metadata and mutation routes accept an optional `project_id` context.
When supplied, a missing or foreign Project fails before a mutation. Tenant and
per-secret metadata/use checks remain canonical, and sensitive mutations retain
the existing admin plus elevated-assurance gate. Omitting Project context retains
the shared administration API. Malformed secret requests receive a generic 422
response; framework validation input values are never echoed in the response.

`GET /api/secrets/{secret_id}/usage?project_id=...` returns a schema `1.0`,
metadata-only projection of canonical TaskSource credentials, typed configuration
revisions, ActionProvider bindings, Model and Agent provider credentials,
extension bindings, bot integration reference fields, execution assignments and
ActionIntents. It never resolves backend material or reads legacy raw credential
fields. Shared workspace consumers and selected-Project consumers are visible;
other Project/resource references contribute an outside-view count without names
or IDs. Configuration and execution history remain labeled with state/revision;
a reference count is not a claim of active use. Noncanonical environment/file
credentials are outside this projection. Key dependency visibility remains a
separate concern tracked by #905.

The projection is computed from canonical stores, not a second dependency ledger.
It bounds scanning at 5,000 records/references and displays at most 100 visible
consumers. Missing configuration schemas or exhausted bounds make impact
unavailable, never falsely empty. Project metadata listing identifies missing,
unauthorized, expired or revoked references in visible consumer configurations
without distinguishing absent from unauthorized secret metadata. Lifecycle
controls require a fresh preview and identify the shared impact beyond the
selected Project before rotation/revocation. This review does not grant authority
or lock out concurrent consumer changes.

Write-only values exist only in the entry field and outgoing broker request.
They are cleared on submission, failed lifecycle attempts, Project changes and
leaving the Secrets surface; they are not copied into dirty-editor snapshots,
browser storage, URLs, telemetry or error messages. Metadata-only audit is labeled
as workspace-wide. Project generation fences reject late reads and prevent a
pending impact lookup from mutating after a Project switch. No LLM calls or new
external provider paths are introduced by this workflow.
