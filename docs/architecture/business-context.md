# Business context, company facts and external record references

## Purpose

The business-context domain adds enough structured company context for Executive/business
reasoning without turning codex-web into a CRM, billing platform, accounting
system, support suite or analytics warehouse.

The canonical boundary stores:

- lightweight BusinessEntity references;
- stable ExternalRecordRef identities;
- selected typed CompanyFacts;
- explicit provenance and source authority;
- links to existing codex-web Resources, Projects, Goals, Decisions, Metrics and
  Evidence.

It does **not** ingest arbitrary provider payloads by default.

## Business entities

BusinessEntity is a tenant/workspace-scoped reference for configured business
concepts such as:

- Customer / Account;
- Product / Product Area;
- Subscription / Commercial Agreement;
- Opportunity;
- Campaign;
- Support Relationship;
- Vendor / Partner;
- Cost Center.

Entities have classification/lifecycle metadata plus bounded display fields and
canonical relationship links. They are references for reasoning and decisions,
not replicas of every field in their source system.

## ExternalRecordRef

ExternalRecordRef identifies one provider-owned record with:

- provider/system and optional provider instance;
- provider object type;
- stable external ID;
- display name/URL only when useful;
- related BusinessEntity, Project and Resource IDs;
- source update/sync timestamps;
- data classification and governed lifecycle.

The stable identity key is tenant/workspace + system + object type + external ID.
The same external identity may exist in another tenant but cannot be duplicated
inside one workspace.

Provider-owned state remains external. Revoking/deleting an ExternalRecordRef
makes facts that depend on it unavailable as current truth until a valid source
is reconciled.

## CompanyFact

CompanyFact stores selected scalar business facts only:

- string;
- number;
- integer;
- boolean.

String facts are bounded. Arbitrary nested provider payloads are intentionally
rejected. Large documents/files remain provider records or Artifacts rather than
CompanyFacts.

A fact records:

- BusinessEntity + fact key;
- value/unit/type;
- source/provider/external record reference;
- source authority and priority;
- quality/confidence;
- effective time/window and observation time;
- freshness policy;
- classification;
- Evidence references;
- supersession links.

Facts are immutable historical records apart from governed lifecycle changes.
Changing a fact creates a replacement through supersession rather than rewriting
history.

## Deterministic source authority and conflicts

Current-fact resolution is deterministic and inspectable. Eligible facts are
ranked by:

1. source authority: authoritative > secondary > observed;
2. configured source priority;
3. quality: verified > normal > partial > suspect;
4. observation time;
5. stable fact ID tie-break.

The selected fact is returned together with all candidate IDs. If multiple
fresh active sources report different values, resolution reports
`conflict=true` and preserves the conflicting fact IDs even though one value is
selected deterministically.

This distinction matters: deterministic selection is **not** the same as saying
the other source agrees.

## Freshness and revoked sources

A fact with `freshness_seconds` becomes stale after that interval and is not
returned as selected current truth.

A fact whose ExternalRecordRef is revoked/redacted/anonymized/deleted is
reported under revoked-source diagnostics and is not selected.

Resolution therefore reports one of:

- fresh;
- stale;
- source_revoked;
- missing.

Executive/business reasoning should receive the resolution diagnostics, not only
the selected scalar value.

## Provenance

Exact provider/reference provenance must survive later reasoning and Decision
snapshots. CompanyFacts retain ExternalRecordRef IDs and Evidence refs; entities
can link to Goals, Decisions, Metrics, Resources and Projects.

Executive integration (#343) should pass these exact references into
Decision/Executive provenance rather than flattening them into unattributed
prose.

## Data governance

Business entities, external record references and facts register with the
existing canonical DataGovernance service.

Derived facts inherit stricter source classification/retention constraints.
Effective governance classification is reflected back into the business-domain
record so the domain and governance registry cannot disagree.

Governance redaction/anonymization/deletion is executed by domain handlers:

- entity identifying/display fields are removed;
- external IDs/display URLs are removed and the source becomes unavailable;
- CompanyFact value is removed and lifecycle becomes inactive.

Raw credentials/keys never belong in business context.

## Tenant isolation

All service/API reads resolve through the authenticated actor's exact
organization/workspace scope. Cross-tenant object lookup returns not found and
tenant lists contain only the caller's workspace data.

Mutation requires administrator + MFA or a service identity with
`business-data:admin`.

Read/query APIs do not confer mutation authority.

## Relationships to existing domains

Business context references existing canonical IDs rather than cloning those
domains:

- Resources remain governed action targets;
- Projects remain engineering/work scopes;
- Goals remain outcome definitions;
- Decisions remain canonical choices/provenance;
- Metrics remain measured KPI definitions/observations;
- Evidence remains structured verification/proof.

Issue #342 can bind business KPI catalogs to these references without introducing
a second Metric engine.

## BusinessDataSource boundary

Issue #341 owns provider-neutral BusinessDataSource ingestion/synchronization and
reconciliation.

BusinessDataSource adapters should:

1. fetch only minimum sufficient fields;
2. create/update ExternalRecordRefs;
3. project selected CompanyFacts;
4. retain cursor/source provenance;
5. resolve deletions/revocations explicitly;
6. never make provider text authoritative merely because it was returned by an
   integration.

The domain in this document remains provider-neutral.

## Failure semantics

- duplicate external identity -> conflict;
- missing linked entity -> not found;
- invalid/non-scalar fact -> validation failure;
- stale fact -> visible stale result, not silent use;
- conflicting providers -> deterministic selection plus explicit conflict;
- revoked/deleted source -> source_revoked result;
- superseded fact -> historical only;
- governance deletion -> value unavailable;
- cross-tenant reference -> not found.

These are healthy fail-closed states.
