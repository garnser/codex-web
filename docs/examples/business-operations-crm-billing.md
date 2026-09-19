# Worked example: CRM + billing business operations

This scenario uses only synthetic data. It demonstrates both healthy operation
and intentionally failing/recovery states.

## Synthetic company

**Example Co** has one customer:

- canonical entity key: `customer-100`;
- display name: `Northstar Demo Retail`;
- CRM provider record: `acct-demo-100`;
- billing provider record: `cust-demo-100`.

No names, IDs, amounts or provider endpoints in this example are from a real
customer.

## Step 1 — connect the CRM source

Configure the CRM-like source as authoritative for the customer name and ARR:

```json
{
  "name": "Demo CRM accounts",
  "source_type": "reference-crm",
  "source_instance": "crm://synthetic",
  "provider_id": "demo-crm",
  "scope": "accounts",
  "object_type": "account",
  "entity_type": "customer",
  "entity_key_namespace": "customer-key",
  "entity_name_authority": "authoritative",
  "entity_name_priority": 100,
  "credential_ref": "secret://demo/crm",
  "classification": "confidential",
  "field_mappings": [
    {
      "source_field": "arr",
      "fact_key": "arr",
      "value_type": "number",
      "unit": "usd",
      "authority": "authoritative",
      "priority": 100,
      "quality": "verified",
      "freshness_seconds": 86400,
      "classification": "confidential"
    }
  ]
}
```

The reference CRM emits:

```text
entity_key=customer-100
external_id=acct-demo-100
arr=1200
source_sequence=42
```

After sync, Company Operations should show:

- one canonical BusinessEntity;
- one CRM ExternalRecordRef;
- one selected ARR CompanyFact;
- durable cursor/checkpoint 42;
- fresh source health.

## Step 2 — connect a polling-only billing source

Billing uses the same entity key namespace and key so it converges on the same
BusinessEntity, but it retains its own ExternalRecordRef.

Billing reports ARR = 1150 as a secondary source.

Expected result:

- the canonical customer remains one entity;
- CRM and billing provider records remain distinct;
- ARR resolution selects 1200 from the authoritative CRM source;
- `conflict=true` remains visible because billing reports 1150.

Deterministic selection does not erase disagreement.

## Step 3 — define a business KPI

Create an explicit ARR KPI:

```json
{
  "key": "arr",
  "name": "Annual recurring revenue",
  "domain": "revenue",
  "owner_identity_id": "demo-finance-owner",
  "unit": "usd",
  "currency": "USD",
  "freshness_seconds": 86400,
  "direction": "higher_is_better",
  "formula": {
    "kind": "aggregate",
    "terms": [
      {
        "alias": "arr",
        "fact_key": "arr",
        "aggregation": "sum",
        "entity_type": "customer"
      }
    ],
    "left_alias": "arr",
    "scale": 1
  }
}
```

Because the underlying customer ARR has a provider conflict, the KPI refresh is
partial. The MetricObservation is marked partial and the operating view must
not present the KPI as current.

## Step 4 — resolve the provider conflict

Assume the billing integration was mapping the wrong field. Correct that mapping
or source data; do **not** edit the canonical CompanyFact in the browser.

After billing reports 1200 and reconciliation completes:

- both fresh sources agree;
- the ARR KPI refresh is complete;
- Metric currentness becomes fresh/current;
- the company operating view can show CURRENT.

## Step 5 — simulate a provider rate limit

Make the CRM adapter return HTTP 429 with Retry-After=300.

Expected behavior:

- BusinessDataSource status becomes degraded;
- ProviderCapacity shows throttled and retry_at;
- cursor/checkpoint remain on the last fully projected page;
- repeated UI reloads do not bypass retry state;
- no model call is needed to diagnose the condition.

After the retry window and provider recovery, bounded reconcile resumes from the
durable cursor.

## Step 6 — simulate credential revocation

Revoke the underlying secret used by `secret://demo/crm`.

Expected operator flow:

1. Company Operations reports source/auth failure;
2. update/rotate the SecretReference through secret administration;
3. do not paste the credential into a BusinessDataSource record;
4. run bounded reconcile;
5. verify fresh source health and current facts.

## Step 7 — Goal and Decision provenance

Bind the ARR KPI to:

- Goal `goal-demo-growth`;
- Decision `decision-demo-budget`.

Capture an operating snapshot.

The persisted target snapshot pins:

- KPI revision;
- Metric revision;
- MetricSnapshot ID;
- exact MetricObservation IDs;
- explicit target window where configured.

Later reviews do not need to reconstruct measured state from prose.

## Step 8 — Executive reasoning

A CRO/CFO Executive activation should receive only the bounded canonical
business context relevant to its role:

- selected BusinessEntity IDs;
- fact resolution including conflict/freshness;
- KPI revision + Metric revision + observation IDs;
- Goal/Decision references;
- Evidence and memory allowed by governance.

A stale/conflicted KPI remains uncertainty. The Executive must not convert it
into a current measured fact.

## Step 9 — external mutation

Suppose the recommendation is "offer a revised subscription".

That recommendation does **not** call the billing SDK through BusinessDataSource.

The mutation path is:

```text
Executive recommendation
-> Decision / Work
-> ApprovalRequest when required
-> ActionIntent
-> authorized billing ActionProvider
-> provider receipt
-> verification/Evidence
```

If the provider returns an unknown outcome, the ActionIntent enters
requires_reconciliation. Do not blind-retry a non-idempotent provider action.

## Recovery table

| Failure | Visible state | Recovery |
| --- | --- | --- |
| stale source | fact/KPI stale | restore source and reconcile from durable cursor |
| provider disagreement | CompanyFact conflict | correct authority/mapping/source data; never hide disagreement |
| revoked credential | source auth failure | rotate SecretReference, then reconcile |
| missing metric input | KPI partial/missing | repair upstream fact projection |
| failed reconciliation | source degraded + last_error | fix cause, retry from unchanged cursor |
| provider outage | ProviderCapacity unavailable | wait/restore provider; cursor remains durable |
| rate limit | ProviderCapacity throttled | respect retry_at |
| denied Action authority | ActionIntent blocked/denied | change authority/policy through canonical admin path |
| expired approval | ApprovalRequest expired | create a new current approval request |
| unknown action result | requires_reconciliation | use canonical action reconciliation, not blind retry |

The checked-in `tests/fixtures/company_operations_synthetic.json` fixture
contains these same kinds of deliberately synthetic/failing states.
