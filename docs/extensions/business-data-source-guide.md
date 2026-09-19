# Build a governed BusinessDataSource connector

This guide shows how to add a read/sync business-system connector without
accidentally granting external mutation authority.

## 1. Pick the provider boundary first

A BusinessDataSource is for **ingestion and synchronization** only. It may:

- discover records;
- page through records;
- read one provider record;
- consume incremental change cursors;
- normalize provider events/webhooks;
- emit tombstones for deleted provider records.

It must not update the CRM, send customer messages, change subscriptions,
modify campaigns, issue credits, or perform other provider-side mutations.

Those effects belong behind ActionProvider + ActionIntent, with canonical Role
authority, ApprovalRequest, Resource scope and Evidence.

## 2. Declare capabilities honestly

A CRM-like adapter might declare:

```python
capabilities = BusinessDataSourceCapabilities(
    frozenset({
        BusinessDataSourceCapability.DISCOVERY,
        BusinessDataSourceCapability.PAGED_DISCOVERY,
        BusinessDataSourceCapability.INCREMENTAL_SYNC,
        BusinessDataSourceCapability.READ,
        BusinessDataSourceCapability.EVENTS,
        BusinessDataSourceCapability.TOMBSTONES,
    })
)
```

A billing/support/analytics adapter may only support:

```python
capabilities = BusinessDataSourceCapabilities(
    frozenset({
        BusinessDataSourceCapability.DISCOVERY,
        BusinessDataSourceCapability.PAGED_DISCOVERY,
        BusinessDataSourceCapability.READ,
    })
)
```

Do not emulate a capability that the provider cannot implement safely.

## 3. Normalize minimum-sufficient records

Adapters return `BusinessDataSnapshot`, not provider SDK objects or arbitrary
JSON payloads.

A snapshot should contain:

- stable provider object type;
- stable external ID;
- a tenant-defined cross-source business entity key;
- display/entity name where required;
- selected bounded scalar fields only;
- provider update timestamp/sequence/revision when available;
- tombstone flag when deletion semantics are known.

Example:

```python
BusinessDataSnapshot(
    object_type="account",
    external_id="acct-demo-100",
    entity_key="customer-100",
    entity_name="Northstar Demo Retail",
    fields=(
        BusinessDataField(source_field="arr", value=1200.0),
        BusinessDataField(source_field="active", value=True),
    ),
    source_updated_at=1_900_000_000.0,
    source_sequence=42,
    source_revision="rev-42",
)
```

Do not add secrets, credentials, opaque documents, raw webhook payloads, or
large nested objects.

## 4. Map selected fields into CompanyFacts

The source configuration owns the mapping from provider field to canonical fact:

```json
{
  "source_field": "arr",
  "fact_key": "annual_recurring_revenue",
  "value_type": "number",
  "unit": "usd",
  "authority": "authoritative",
  "priority": 100,
  "quality": "verified",
  "freshness_seconds": 86400,
  "classification": "confidential"
}
```

The mapping is explicit because source authority is a policy/configuration
choice, not something inferred from provider name.

If CRM and billing disagree, codex-web preserves both facts and reports the
conflict even when deterministic authority rules select one current value.

## 5. Credentials stay behind SecretReference

Store only `credential_ref` on the BusinessDataSource configuration. Never put
API keys/tokens in:

- BusinessDataSnapshot fields;
- CompanyFacts;
- Extension configuration values intended for model context;
- logs;
- screenshots;
- fixtures.

The adapter factory resolves credentials through the existing secret boundary.

## 6. Cursor and checkpoint rules

The runtime advances the durable reconciliation cursor only after the full page
has been normalized, committed as canonical events, and projected.

Provider webhooks do not advance the durable reconciliation cursor. Webhook
delivery can be out of order and provider cursors can be opaque.

On a provider outage, rate limit, projection error, or process crash:

- the last fully committed cursor remains unchanged;
- ProviderCapacity records retry/backoff state;
- the next run resumes from the previous durable position.

## 7. Tombstones and source deletion

A tombstone revokes the corresponding ExternalRecordRef. Historical facts remain
for audit but are excluded from current truth.

A provider tombstone is **not** the same thing as governance deletion.
Redaction/anonymization/deletion still goes through DataGovernance so retention
and legal-hold policy cannot be bypassed.

## 8. Extension manifest and registration

When shipping a connector as an extension, declare the BusinessDataSource
capability in the extension package and register the adapter factory only while
the package is:

- installed;
- compatible;
- enabled;
- not quarantined;
- granted the necessary non-secret configuration/credential references.

Installation does not automatically grant provider mutation authority.

A minimal extension package should include:

```text
my_business_source/
  manifest.template.json
  build.py
  adapter.py
  README.md
```

The manifest should identify package/version/compatibility and only the
capabilities the connector actually needs. Use the generic extension lifecycle
and configuration contracts; do not invent connector-specific trust state.

## 9. Conformance expectations

A connector test suite should cover at least:

- declared capabilities match implemented methods;
- pagination reaches every fixture record exactly once;
- incremental cursor replay is idempotent;
- duplicate event delivery is idempotent;
- out-of-order events cannot overwrite newer provider position;
- tombstone revokes the source;
- provider outage/rate limit preserves cursor;
- unsupported capability fails visibly;
- raw credentials are absent from normalized snapshots/logs;
- cross-tenant source state is inaccessible;
- connector code exposes no direct provider-write method.

The repository reference fixtures demonstrate two materially different source
categories:

- `ReferenceCRMDataSource`: delta/event/tombstone capable;
- `ReferenceBillingDataSource`: polling/paged-read only.

See `tests/test_business_data_sources.py` for their shared conformance behavior.

## 10. Production rollout checklist

Before enabling a source:

1. define the business entity key namespace;
2. define source authority per projected field;
3. define data classification and freshness;
4. verify SecretReference access;
5. verify extension compatibility and grants;
6. run bounded discovery/reconcile in a non-production workspace;
7. inspect ExternalRecordRef/CompanyFact provenance;
8. intentionally introduce one conflict and verify it is visible;
9. simulate provider outage/rate limit and verify cursor preservation;
10. only then enable scheduled reconciliation.

Do not connect ActionProvider capabilities until the read/sync path is
observable and trustworthy.
