# Compatibility, versioning, and migration policy

## Status

**Milestone 3 platform contract.** This policy applies to public/admin APIs, domain schemas, persisted records, execution/task/action contracts, provider adapters, canonical events, and UI-consumed payloads.

The central rule is fail-closed interpretation: codex-web must never silently reinterpret an unknown contract version as a known one.

## Version model

Platform contracts use `major.minor` versions. `ContractSpec` declares one current version and the exact versions a process accepts. Exact support is intentional: a producer cannot assume that an older consumer will safely ignore new fields or semantics merely because the major version is unchanged.

- **Patch-only implementation fixes** do not change a contract version when serialized/API semantics are unchanged.
- **Additive minor changes** increment the minor version when a consumer can deliberately support both old and new forms.
- **Breaking changes** increment the major version.
- A version is accepted only after it is explicitly listed in that process's compatibility window.
- Deprecated versions remain accepted only while explicitly listed; the compatibility manifest identifies them.

Current foundational contracts are published by `GET /api/compatibility`:

- HTTP API: `1.0`;
- canonical event envelope: `1.0`;
- TaskSource adapter/event contract: `1.0`;
- ActionProvider contract: `1.0`;
- generic persisted-record envelope: `1.0`.

Clients may supply `api_version` to `/api/compatibility` to test compatibility. Unsupported versions return a structured `409 api_version_unsupported` response rather than falling through to a guessed interpretation.

## Public and admin APIs

Existing `/api/...` endpoints constitute API contract 1.0. Within that contract:

- adding an optional response field is permitted only when it does not alter the meaning of existing fields;
- adding a new endpoint is additive;
- removing/renaming a field, changing units/types/authorization meaning, changing default mutation semantics, or making an optional field required is breaking;
- breaking HTTP semantics require a new declared API version before rollout;
- API consumers should use the compatibility manifest rather than infer support from application build versions.

Authentication/authorization tightening required to fix a security defect is not delayed for compatibility; it must be documented as a security boundary change and fail closed.

## Canonical events

`CanonicalEventEnvelope` is the platform event boundary for new canonical producers/consumers. It contains:

- `schema_version`;
- stable `event_id` and `event_type`;
- `occurred_at` and `source`;
- optional correlation/causation ids;
- optional future tenant/workspace scopes;
- a structured payload.

Construction validates `schema_version` immediately. An unsupported event version is rejected before payload interpretation. Future event-bus work must transport this envelope or an explicitly versioned successor rather than unversioned dictionaries.

Provider events normalized through `TaskSourceEvent` now carry the TaskSource schema version and reject unsupported versions at the provider-neutral boundary.

## Adapter and client negotiation

`TaskSource` declares a `contract_version` in addition to capability flags. `TaskSourceRegistry` validates the adapter contract before returning it to canonical work-item code. An incompatible adapter becomes a visible `TaskSourceResolutionError`; core code does not attempt best-effort reinterpretation.

For the initial migration, in-tree/legacy extension adapters that predate the field and omit `contract_version` are treated as TaskSource 1.0. This compatibility shim is explicit and documented; adapters declaring a version must match the supported contract. The shim can be deprecated only after supported adapters have migrated.

Capabilities remain orthogonal to versioning: a version-compatible adapter can still lack a particular operation, and `TaskSourceCapabilities.require()` remains the deterministic operation gate.

## Persisted schemas and migrations

New durable schemas that can evolve independently should persist an explicit schema version. `VersionedRecord` provides the minimal 1.0 envelope where a domain-specific model does not yet exist.

`MigrationRegistry` provides deterministic forward migration hooks:

1. each step names an exact source and newer target version;
2. duplicate steps and backwards migrations are rejected;
3. migration paths are resolved deterministically;
4. missing paths fail visibly with `ContractCompatibilityError`;
5. reaching the target version is a no-op, allowing idempotent restart/replay behavior;
6. migrations transform copied mappings rather than mutating caller-owned payloads.

Domain migrations should keep code-level validators/interpreters in code and mutable business definitions in data, consistent with the repository Definitions Principle.

During rolling upgrades, readers must support every persisted version that can legitimately remain on disk. Writers should emit the current version only after all readers required by the rollout support it. Rollback plans must account for data written by the newer version; where downgrade is not safe, deployment must stop rather than silently truncate or reinterpret data.

## Representative compatibility matrix

| Contract | Current | Older accepted | Future/unknown behavior | Migration behavior |
| --- | --- | --- | --- | --- |
| HTTP API | 1.0 | none yet | manifest negotiation rejects unsupported request | introduce explicit successor before breaking semantics |
| Canonical event | 1.0 | none yet | construction/consumer boundary rejects | register event migration/consumer compatibility deliberately |
| TaskSource | 1.0 | omitted version treated as legacy 1.0 during migration | registry rejects declared unsupported version | adapter upgrade is explicit; capabilities negotiated separately |
| ActionProvider | 1.0 | none yet | registry rejects declared unsupported version | provider implementations upgrade explicitly; capabilities remain separately declared |
| Persisted record | 1.0 | domain-specific legacy loaders may seed 1.0 | envelope rejects unsupported version | use `MigrationRegistry` step chain |
| Execution contract | 1.4 | governed by its own exact schema validator | Pydantic/literal validation rejects | introduce and test explicit new execution-contract schema |

## Deprecation policy

Deprecation is a lifecycle, not silent tolerance:

1. add the successor and compatibility tests;
2. mark the old version deprecated in its `ContractSpec`;
3. expose both versions in the compatibility manifest;
4. migrate stored records/adapters/clients;
5. remove old support only in a release that documents the compatibility break or after the documented support window;
6. retain migration/replay capability for historical audit data where required.

## Conformance tests

Compatibility tests must cover, as applicable:

- supported previous/current version acceptance;
- future major/minor rejection;
- malformed version rejection;
- deterministic migration path selection;
- missing-path failure;
- target-version idempotence;
- adapter compatibility and capability negotiation;
- event-envelope version rejection before payload interpretation.

`tests/test_compatibility.py` supplies the initial conformance suite. Future versioned domains should extend this suite or add equivalent focused tests before changing their declared compatibility window.

## UI impact

This issue has no new operator workflow requiring a dedicated UI. The canonical machine-readable compatibility manifest is an API/platform surface. Platform administration UI work in #141 can project that manifest later; no UI-local version registry should be created.
