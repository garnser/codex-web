# Canonical resource catalog

Codex-web authority and execution must target stable resource identities rather than treating provider names, URLs, repository paths, account numbers, or environment labels as durable authorization keys.

## Resource identity

A `Resource` has a stable codex-web ID and tenant scope. The initial catalog supports repositories, services, environments, deployment targets, cloud accounts/subscriptions, databases, and an extensible `other` type.

Provider-specific identifiers live in aliases and provenance:

- aliases provide resolvable legacy/provider names;
- provenance records provider, provider instance, external ID/URL, and discovery timestamps;
- provider rename/move updates aliases/provenance without changing the canonical resource ID.

Exact duplicate aliases within the same namespace/provider are rejected. Unqualified resolution that matches more than one canonical resource fails closed and requires a stable ID or additional namespace/provider/type qualification.

Disabled/deleted resources remain auditable metadata but cannot resolve as privileged execution targets.

## Tenant and ownership boundary

Every resource, relationship, and project binding carries organization/workspace scope. Cross-tenant lookup behaves as not found where possible; cross-tenant relationship/binding mutation is denied.

Resource mutation requires canonical resource-administration authority. This catalog does not introduce a second identity/role system.

Resources also carry owner identity, lifecycle, sensitivity, and risk classification for later policy, approval, governance, and audit packages.

## Relationships

Relationships form a tenant-local directed graph. Supported initial relationship semantics are:

- `contains`
- `depends_on`
- `implements`
- `deploys_to`
- `hosted_in`
- `connects_to`

Traversal is deterministic, cycle-safe, depth bounded, tenant scoped, and can be filtered by relationship type and direction. Invalid traversal directions fail closed.

A typical chain can be:

`Project -> Repository -> Service -> Environment -> Deployment Target`

Project-to-resource association is stored as an explicit `ProjectResourceBinding`, keeping Project as its existing canonical domain while resources remain independently stable.

## Multi-repository Project targeting

A Project may bind multiple active repository Resources. Repository authority for
one execution is represented by a canonical `RepositoryExecutionTarget`, which
records tenant/project scope, one mutable repository (or an explicit
orchestration-only no-mutation target), optional read-only repository context,
selection source, and source provenance.

Target selection is deterministic and never delegated to a model. Precedence is:

1. canonical Work Item repository Resource;
2. explicit execution/thread target;
3. thread/execution-profile target;
4. an authorized Project binding with purpose `execution-default`;
5. the compatibility fallback when exactly one active repository is bound.

Multiple active repositories with no deterministic selector fail with
`repository_target_ambiguous` before an ExecutionWorkspace or
ExecutionAssignment is created. A requested repository outside the active
Project/tenant binding fails as unauthorized.

Thread bootstrap persists the selected target onto the canonical execution
assignment and thread settings. A live bootstrap-bound thread cannot silently
switch mutable repositories; changing repository scope requires a new compatible
thread/execution boundary. This preserves the worker/workspace lease authority
instead of treating a UI selection as authorization.

## Legacy migration and aliases

`migrate_legacy_strings()` converts existing repository/environment strings into canonical resources using `legacy` aliases and binds them to the owning Project. Re-running the migration is idempotent.

Provider aliases and old names may be retained after rename so existing configuration can be reconciled without changing authorization semantics.

## Execution contracts

Work Items carry `resource_ids`. Task-source projection inherits active resource
IDs bound to the owning Project. When a provider identity deterministically
identifies one repository, projection narrows that set through the canonical
resource alias/provenance catalog instead of granting unrelated sibling
repositories as execution targets.

Execution contract schema **1.2** adds `target.resource_ids` while retaining the legacy repository string as compatibility context. Canonical resource IDs are deduplicated before dispatch.

Privileged execution/policy packages should prefer `resource_ids`; mutable provider strings must not silently become authority.

## API

The catalog exposes:

- `GET/POST /api/resources`
- `GET/PATCH /api/resources/{resource_id}`
- `GET /api/resources/resolve`
- `POST /api/resources/relationships`
- `GET /api/resources/{resource_id}/relationships`
- `GET /api/resources/{resource_id}/traverse`
- `POST /api/resources/project-bindings`
- `GET /api/projects/{project_id}/resources`
- `POST /api/resources/migrate-legacy`

The API is a projection over canonical catalog state; UI work in #141 should consume these endpoints rather than creating UI-owned resource mappings.
