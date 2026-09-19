# Definition Registry

The Definition Registry is the canonical database-backed store for mutable operational definitions. The architectural rule is:

> **Definitions are data. Schemas, interpreters, migrations, and hard security invariants remain code.**

Definitions are not configuration values, permissions, secrets, entitlements, or current runtime state. They may be referenced by those domains, but a stored payload cannot introduce executable Python or weaken code-owned structural controls.

## Record identity and history

Every definition revision records:

- stable `definition_id` and `kind`;
- definition schema version and registry record schema version;
- immutable revision number and record ID;
- global/organization/workspace/project scope;
- lifecycle state;
- validated payload checksum;
- creator/validator/publisher, timestamps, reason and approval metadata;
- effective dates and engine compatibility bounds;
- supersession and rollback provenance;
- optional `derived_from_record_id` provenance for typed/clone-derived drafts.

The checksum covers the stable definition identity, kind, definition schema version and canonical JSON payload. Loading a tampered record fails validation before it can become effective.

Historical revisions remain available for audit/replay. Rollback creates a new revision copied from the selected historical payload; it never rewrites prior history.

## Lifecycle

Supported lifecycle states are:

`draft -> validated -> published -> superseded`

Definitions may also be deprecated, disabled or quarantined. Publication validates through the code-owned schema even if a caller skips the explicit validate step.

Only one published revision may occupy a canonical `kind + definition_id + scope` slot. Publication supports optimistic `expected_active_revision` checks so concurrent editors cannot silently replace a revision they did not review.

A quarantined, missing, corrupt, unsupported-schema or engine-incompatible definition does not fall back to an unrelated stored payload.

## Sensitive publication approval

Definition schemas may register a code-owned publication assessment. The stored
payload cannot change or disable that classifier.

For the canonical `authority-role-catalog`, publication is classified as
sensitive when a candidate expands operational authority, including higher
permission levels, broader resource/project/environment scope, production
access, larger/unbounded monetary or model/token budgets, higher autonomous
risk, weaker approvals, new inheritance, new bindings, or broader/longer
delegation.

Sensitive publication requires a durable independent attestation attached to the
candidate revision. Each attestation records:

- approver identity;
- approval reference and reason;
- candidate checksum;
- the exact active record ID/revision reviewed;
- classifier reasons;
- approval timestamp.

The final publisher must be a different identity from the matching approver.
If the active revision changes after approval, the attestation becomes stale
automatically and cannot authorize publication.

The HTTP approval path requires MFA plus owner/admin/approver membership for
humans, or explicit `definitions:approve` service scope. Global approval is
further restricted to local-trusted platform context (or
`definitions:global-approve` for service principals).

Rollback uses the same gate. If a rollback would expand authority, the registry
creates a normal immutable rollback draft but refuses to activate it. That draft
must receive independent approval and then be published through the standard
path. Rollback therefore cannot bypass the authority-expansion classifier.

Code-owned bootstrap is the sole internal approval bypass and only applies when
seeding an empty canonical slot. Imported records never import publication
attestations; imports become fresh inactive drafts.

## Scope resolution

Published effective definitions resolve deterministically from least to most specific:

1. global
2. organization
3. workspace
4. project

A more-specific matching definition overrides a less-specific definition with the same stable ID/kind. Multiple active records in the same canonical slot are treated as corruption/conflict and fail closed.

The resolver uses a revision-aware in-process cache. Every registry mutation invalidates that cache. Publication/quarantine/bootstrap changes emit a canonical definition-change notification through the runtime EventHub when an event loop is active; long-lived components can therefore reload without LLM polling.

## Schema and engine compatibility

Definition schemas are registered in code with a `kind`, schema version and validator/normalizer. Unknown schema versions are rejected. Records can additionally specify minimum/maximum compatible engine versions.

The registry store itself is versioned using the platform compatibility/migration primitives from #138. Unsupported persisted-store versions or missing migration paths fail visibly.

Application upgrades must validate active definition schemas/engine compatibility before protected work resumes; #168 owns full upgrade/version-skew orchestration.

## Operational Role authority definitions

M6 adds a second code-owned definition schema on the same registry:

- kind: `authority-role-catalog`
- stable ID: `authority.roles.default`
- schema: `1.0`

This catalog is distinct from the execution-role catalog. It stores operational
authorization Roles, atomic grants, identity/Team bindings and expiring
delegations. The evaluator, rank ordering, inheritance validation and fail-closed
security semantics remain code-owned. Runtime decisions retain the exact
Definition Registry reference used.

A project/workspace scoped published authority catalog can override the same
stable global definition through normal registry precedence. Missing or invalid
authority definitions do not fall back to bootstrap data.

## Execution-role migration

The first migrated definition is:

- kind: `execution-role-catalog`
- stable ID: `execution-roles.default`
- schema: `1.0`

The code-owned `ExecutionRoleCatalogDefinition` schema validates role IDs, required structural roles, shared execution rules, owner mappings, Executive defaults, routing keywords, required artifacts, refusal rules, handoff targets, and failure conditions.

The previous Python catalog now lives only in `execution_contract_seed.py` as a bootstrap migration fixture. On an empty installation it is inserted as an ordinary revision-1 published database record. Once any record exists in the global canonical slot, bootstrap does not compete with or overwrite it.

Runtime helpers in `execution_contracts.py` contain only deterministic routing/prompt interpretation logic over a resolved catalog. They do not contain `ROLE_CONTRACTS`, shared-rule constants, or another fallback catalog. An uncomposed runtime with no Definition Registry provider fails visibly.

## Typed Role and execution-contract authoring

The Definition Registry administration surface includes typed editors for:

- `authority-role-catalog@1.0`;
- `execution-role-catalog@1.0`.

Typed authoring is not a second policy store. The editor loads one immutable
registry revision, serializes the registered schema payload, and creates a new
draft through `POST /api/definitions/drafts`. It never publishes or activates a
definition directly.

Derived drafts persist `derived_from_record_id`. The registry validates that
the referenced source exists and belongs to the same definition ID, kind,
scope, and schema version. This makes clone/edit provenance durable rather than
UI-only metadata.

The typed authority editor supports Roles, inheritance, atomic grants,
identity/Team bindings, delegations, environment/resource/project constraints,
financial/token/model-call ceilings, autonomous-risk ceilings, and approval
requirements. The typed execution editor supports contract lane/description,
expected/refused work, artifacts, handoffs, failure conditions, routing
keywords, auto-selection, shared execution rules, and owner/default mappings.

Both Role schemas expose a lifecycle value:

- `active` — normal runtime behavior;
- `deprecated` — retained for existing/direct use but discouraged from new
  automatic execution routing;
- `disabled` — contributes no operational authority or execution-role
  resolution.

Disabled operational authority Roles are skipped by the canonical authority
evaluator even if stale bindings/delegations still reference them. Reactivating
a disabled Role, or returning a deprecated Role to active, is classified as a
sensitive authority expansion and therefore passes through the independent
publication-approval gate.

Disabled execution Roles are excluded from resolution/routing. Code-owned
structural roles (`orchestrator`, `quinn`, and `release-manager`) cannot be
disabled, and owner/default mappings cannot target disabled execution Roles.

Lifecycle changes are ordinary immutable definition drafts followed by the
normal validate/approval/publish lifecycle. Published history is never edited in
place, and no scope-level definition is silently removed in a way that could
expose a broader fallback definition.

For backward compatibility, schema-1.0 records created before Role lifecycle
existed retain their original normalized payload/checksum shape. Missing
lifecycle is interpreted as `active`; new typed drafts persist lifecycle
explicitly.

## Execution attribution

Every runtime-generated `ExecutionContractV1` includes exact Definition Registry references:

- definition ID and kind;
- revision;
- immutable registry record ID;
- payload checksum;
- definition schema version.

When a work item is actually dispatched, that exact reference is also pinned into `WorkItemState.execution.definition_refs` and an `execution_definition_pinned` event is emitted. This makes the definition set recoverable from canonical execution state even after a newer revision is published.

The Definitions API can report current work-item usages of an exact record. Future Goal, Decision, ActionIntent, model, worker and audit domains should register additional usage providers rather than copy definition payloads.

## Bootstrap, import/export and recovery

Bootstrap is idempotent per canonical definition slot. Seed data becomes normal registry history after first insertion.

Export uses a versioned `codex-web-definitions` document. Import validates checksums/schema compatibility and creates new drafts rather than trusting imported lifecycle/authority metadata or directly activating external records.

If the execution-role definition is missing, unpublished, quarantined, corrupt or incompatible, execution-role resolution fails rather than consulting the bootstrap seed. Operators may inspect registry/bootstrap state and restore/publish a known-good revision through canonical APIs.

## API

The canonical administration API is under `/api/definitions`:

- `GET /schemas` — code-owned supported definition schemas;
- `GET /bootstrap` — registry/bootstrap health metadata;
- `GET /records` — browse/filter full history;
- `POST /drafts`;
- `POST /{record_id}/validate`;
- `GET /{record_id}/publication-assessment`;
- `POST /{record_id}/publication-approvals`;
- `POST /{record_id}/publish`;
- `POST /{record_id}/quarantine`;
- `POST /rollback`;
- `POST /resolve`;
- `GET /diff?left=...&right=...`;
- `GET /{record_id}/usage`;
- `GET /export`;
- `POST /import`.

The Platform Foundation UI (#141) should build browse/search, history, draft/validate/publish/supersede/rollback, diff, provenance, compatibility, usage/reference and impact experiences on these APIs. Raw SQLite editing is not a supported administration path.

## Security boundary

A database definition cannot:

- execute arbitrary code;
- grant itself authority;
- bypass tenant scope;
- reveal secret/key material;
- change parser/interpreter semantics;
- change cryptographic verification;
- weaken hard fail-closed constraints.

Sensitive Definition publication approval is enforced by code-owned schema classifiers and durable candidate-bound attestations. The Definition Registry remains distinct from runtime Role evaluation: stored policy can require or constrain authority, but cannot weaken the publication classifier or hard fail-closed semantics.
