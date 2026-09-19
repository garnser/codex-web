# Data governance

## Status

Canonical data-governance foundation for issue #140.

This contract establishes one canonical metadata and enforcement boundary for data classification, retention, privacy actions, legal holds, export authorization, residency constraints, and model-context filtering.

It does **not** create a second storage system for prompts, threads, Work Items, memory, artifacts, logs, or provider payloads. Domain stores retain ownership of their payloads. The governance registry stores only the metadata required to make deterministic policy decisions and to audit governed actions without copying sensitive content.

## Classification

Canonical sensitivity levels are ordered:

```text
public < internal < confidential < restricted < secret
```

The order is code-owned because lowering the meaning of a sensitivity level through mutable configuration would weaken a structural control.

Durable domains register a `GovernedDataRecord` containing:

- tenant/workspace ownership;
- optional project scope;
- object type and canonical object ID;
- data category;
- requested and effective classification;
- optional retention-policy reference;
- retention expiry and governed action;
- residency tags;
- source/derived-record references;
- model-context denial state;
- legal-hold state;
- lifecycle and action timestamps;
- actor/provenance metadata.

The registry never stores the governed payload itself.

## Derived-data propagation

Derived summaries, memory, evidence, and other records may reference their source governance records.

Resolution is deterministic:

1. Effective classification is the maximum sensitivity of the requested classification and all source records.
2. A derived record cannot outlive the earliest explicit source retention expiry.
3. Residency tags are the union of the derived and source requirements.
4. A source-level model-context denial propagates to derived data.
5. Credential-category records always deny model-context use.

This prevents a summary or memory object from silently becoming less protected than the material used to create it.

## Retention and legal hold

Retention expiry does not directly erase payloads from arbitrary domain stores.

A retention sweep creates a canonical governed-action request using the record's configured retention action. Legal holds prevent execution and keep the request visibly blocked.

Actual redaction, anonymization, or deletion may be reported as complete only after the owning domain has registered an action handler and that handler succeeds. If no handler exists, execution fails closed with `adapter_unavailable`.

This avoids a dangerous state where governance metadata says an object was deleted while the source payload still exists.

## Governed deletion and redaction

The action lifecycle is:

```text
request
  -> pending
  -> blocked (legal hold / adapter unavailable)
  -> pending after the blocker is cleared
  -> completed after the domain handler succeeds
```

Completed actions record only an adapter receipt/reference, not the deleted or redacted content.

Domain integrations should make action handlers idempotent because retries can occur after uncertain failures.

## Model-context filtering

Before sensitive governed data enters model context, callers can use the deterministic context-filter API.

The baseline structural rules are:

- credential-category data is never eligible for model context;
- `secret` data is never eligible for model context;
- a record may explicitly deny model-context use;
- inactive/redacted/anonymized/deleted records are denied;
- the caller supplies a maximum allowed classification and higher classifications are denied.

Future policy may further restrict routing by model/provider/residency, but mutable policy must not be able to weaken the baseline credential/secret exclusions.

## Export authorization

The governance API returns an **export manifest**, not the data itself.

The manifest identifies allowed canonical object references, classification, project scope, and residency tags. Domain APIs remain responsible for exporting the actual authorized content.

Credential and `secret` records are denied by the baseline export gate. Every allow/deny decision emits metadata-only governance audit evidence.

## Audit

Governance events intentionally contain only:

- tenant/workspace;
- actor;
- operation/outcome;
- governed record/request IDs;
- object type/object ID;
- classification;
- reason code;
- timestamp.

They must not contain prompt text, secret values, deleted content, exported content, or arbitrary user prose.

## API

```text
GET    /api/data-governance/records
POST   /api/data-governance/records
GET    /api/data-governance/records/{record_id}
POST   /api/data-governance/records/{record_id}/legal-hold
DELETE /api/data-governance/records/{record_id}/legal-hold

GET    /api/data-governance/actions
POST   /api/data-governance/actions
POST   /api/data-governance/actions/{request_id}/execute

POST   /api/data-governance/retention/sweep
POST   /api/data-governance/context/filter
POST   /api/data-governance/exports/authorize
GET    /api/data-governance/events
```

Mutating governance operations require a tenant administrator or a service principal with the `data-governance:write` scope. Read/filter operations remain tenant-scoped.

## Definition Registry relationship

Retention-policy and residency-policy definitions are expected to be versioned mutable definitions once the Definition Registry (#170) is available. This foundation therefore stores an opaque `retention_policy_ref` on governed records rather than creating a competing mutable policy-definition store.

Classification ordering, credential/secret model-context exclusions, tenant isolation, and the requirement that source-domain deletion actually succeed before completion are code-owned structural controls.

## Artifact and evidence adoption

Artifact/evidence metadata is the first durable domain integrated with this boundary.

New artifacts register an `artifact` governance record and new evidence registers an `evidence` record. Evidence references the governance records of its source artifacts, so its effective classification, retention ceiling, residency constraints, and model-context restrictions cannot be weaker than the artifacts used to produce it.

Existing artifact/evidence rows predate governance metadata. The administrator-only `POST /api/artifact-evidence/governance/sync` migration endpoint links them to canonical governance records. Because historical sensitivity was not recorded, legacy rows are conservatively backfilled as `confidential`.

Governed redaction/anonymization/deletion for these object types scrubs the payload-bearing metadata stored by codex-web, invalidates dependent evidence/verifications, and preserves canonical IDs as tombstones for referential/audit integrity. A `delete` receipt from this adapter **does not claim that an external Git commit, CI job, deployment, or provider object was deleted**. External side effects require their own canonical ActionProvider/ActionIntent path.

## Adoption path

This foundation is intentionally domain-neutral. Existing domains should adopt it incrementally:

1. register governance metadata when durable governed objects are created;
2. reference source records for derived summaries/memory/evidence;
3. call the context filter before model/provider routing;
4. implement domain action handlers for redaction/anonymization/delete;
5. use export authorization before domain content export;
6. expose governance status and action blockers in the Platform Foundation administration UI (#141).

A domain is not allowed to claim governed deletion is complete until its source payload handler has actually succeeded.
