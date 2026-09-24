# Typed configuration and feature rollout

Codex-web configuration is canonical runtime/deployment data with deterministic scope precedence and provenance. It is deliberately separate from definitions, policy, secrets, entitlements, and operational state.

## Concept boundaries

- **Configuration** selects typed runtime/deployment values and rollout state.
- **Definitions** are reusable versioned domain/runtime descriptions managed by the Definition Registry.
- **Policy** grants or denies authority and imposes constraints/approvals. Configuration cannot grant authority.
- **Secrets** are referenced by stable secret IDs. Raw secret values are not configuration.
- **Entitlements/quotas** describe hosted/service access and consumption boundaries.
- **Canonical state** describes current domain/operational facts and is not overwritten by configuration precedence.

A configuration value may reference a Definition Registry revision or secret reference, but it does not copy or become authoritative for the referenced object.

## Code-owned specs, data-owned values

Each key is registered with a code-owned `ConfigurationSpec`. The spec defines:

- canonical key and value kind;
- default or required/unset behavior;
- allowed scopes;
- hot-reload vs startup-only behavior;
- whether the key is a boolean feature flag;
- whether an emergency force-disable kill switch is supported.

Specs are schemas/invariants and remain code-owned. Mutable values are versioned database records.

The schema rejects any spec that claims `grants_authority=true`. A feature flag can expose/configure functionality but cannot bypass RBAC, agent authority, approvals, tenant isolation, or any other policy gate.

## Scopes and precedence

Effective values resolve deterministically from least to most specific:

1. deployment
2. global
3. organization
4. workspace
5. project
6. resource

Only records matching the current canonical scope IDs participate. The most-specific matching published record wins. If no record applies, the code-owned default is used. A required key with neither a record nor default fails closed.

Every effective published value identifies its record ID, schema version, revision, scope, publishing actor/time/reason, and whether it is hot-reloadable or startup-only.

## Lifecycle and optimistic concurrency

Configuration revisions are append-only historical records with lifecycle states including draft, published, superseded, and disabled.

Normal mutation flow:

1. create a typed draft;
2. validate it against the registered spec;
3. preview scope impact;
4. publish using the expected active revision where concurrent administration is possible;
5. supersede the previous published revision at the same key/scope.

Removing an explicit override is also versioned: reset creates a disabled tombstone that supersedes the active revision at that exact scope. Resolution then falls through to the next applicable published scope or the code-owned default. Reset never writes null and never deletes history.

Rollback does not rewrite history. It creates and publishes a new revision containing the selected historical value and records the rollback source revision.

The persisted registry is schema-versioned and uses the platform migration registry. Missing/newer unsupported schema paths fail visibly rather than being reinterpreted.

## Feature targeting

Boolean feature flags may use deterministic rollout metadata:

- percentage;
- named cohorts;
- expiry timestamp;
- owner metadata.

Percentage assignment hashes the key, immutable record ID, and subject ID, so the same subject receives the same result for a given published revision. Missing subject identity cannot accidentally enter a partial percentage rollout.

Expired or nonmatching targeted records are ignored and normal scope/default resolution continues.

A `force_disabled` record is available only for kill-switch-capable flags, must have value `false`, and cannot be cohort/percentage targeted. A matching kill switch overrides more-specific enablement; it is an operational safety control, not an authority decision.

## Reference-valued configuration

`secret_ref` accepts only a structured secret reference. It cannot hold a raw credential.

`definition_ref` accepts only a stable Definition Registry ID and explicit revision. Runtime code must resolve that reference through the Definition Registry rather than embedding a copied definition in configuration.

As the secret broker and Definition Registry land, their canonical reference models may replace these narrow reference schemas without changing the rule that configuration contains references rather than protected/duplicated payloads.

## API

The canonical service is exposed through `/api/configuration`:

- `GET /specs`
- `GET /records`
- `POST /drafts`
- `POST /{record_id}/validate`
- `POST /{record_id}/publish`
- `POST /rollback`
- `POST /reset`
- `POST /resolve`
- `GET /{record_id}/impact`

The platform administration UI should consume these APIs and must visually distinguish configuration/feature rollout from definitions, policy, secrets, entitlements, and current runtime state.

## Migration rule

New runtime settings should register a typed spec and resolve through `ConfigurationService` rather than adding ad-hoc environment/UI precedence. Existing domain-specific persisted documents should be migrated only when they are truly runtime configuration; provider/domain state should remain in its owning domain model.

Startup-only settings must be presented as requiring restart/reload. The UI must not imply that publication makes them hot immediately.

## Security and observability

Configuration records contain actor/time/reason provenance but are not a substitute for the immutable autonomy/security audit introduced later. Sensitive administration will additionally require human identity, step-up authentication, and canonical role-authority controls.

Configuration resolution is deterministic and must never require an LLM.
