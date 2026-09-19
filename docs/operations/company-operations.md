# Company Operations workspace and business integration diagnostics

Company Operations is a deterministic operator workspace over the canonical
business-data, KPI, Goal, Decision, Executive, Attention, ApprovalRequest,
ActionIntent and Evidence domains.

It does not introduce a second copy of authority, synchronization state, KPI
arithmetic or action lifecycle.

## What operators inspect

The workspace summarizes:

- configured business domains;
- canonical BusinessEntity records;
- current CompanyFact resolution, including stale/missing/revoked/conflicting
  source state;
- ExternalRecordRef provider identities and deep links;
- BusinessDataSource adapter/extension identity, capability declaration,
  configuration references, credential reference, cursor/checkpoint and health;
- provider capacity, retry time and consecutive failures;
- deterministic drift/conflict diagnostics;
- canonical business KPI currentness and revision provenance;
- active Goals, Decisions and Executive activations;
- current human AttentionItems and ApprovalRequests;
- active/uncertain ActionIntents.

The browser renders these server-owned results. It does not recompute authority,
fact conflict resolution, Metric currentness, source cursor state or provider
capacity.

## Canonical facts versus provider records

BusinessEntity and CompanyFact cards are labeled as canonical codex-web state.
ExternalRecordRef cards are separately labeled provider references.

A provider record remains authoritative for provider-owned fields where
configured. codex-web only projects the minimum governed facts required by
Goals, Decisions, KPI calculation, Executive reasoning, policy and Actions.

## BusinessDataSource controls

Company Operations exposes bounded controls backed by the existing
BusinessDataSource API:

- sync: at most five pages from the durable cursor;
- bounded full reconcile: restart discovery but process at most five pages per
  request;
- pause;
- quarantine;
- activate.

These controls do not bypass the BusinessDataSource capability contract,
ProviderCapacity blocking, Scheduler state or admin/MFA/service-scope checks.

The UI never edits a cursor/checkpoint directly.

## Extension diagnostics

When a BusinessDataSource is linked to an extension installation, diagnostics
show:

- extension package ID/version;
- extension lifecycle and health;
- requested package capabilities;
- currently active granted capabilities;
- configuration record IDs;
- BusinessDataSource adapter capabilities.

The extension's requested capabilities and active grants are deliberately shown
as separate concepts.

Quarantined, incompatible, disabled or removed linked extensions block source
health in Company Operations. A source without an extension link is treated as a
built-in/runtime-registered adapter.

## Explain source to consequence

The read-only explain view reconstructs canonical state, without a model:

1. source request/event;
2. governed BusinessEntity/CompanyFact inputs;
3. canonical business KPI/Metric state;
4. deterministic Executive role selection;
5. advisory Executive recommendation/proposal;
6. materialized Goal/Decision/Work reference;
7. ApprovalRequest / authority gate;
8. ActionIntent, including risk class, resource scope and authority level;
9. provider receipt and external result;
10. verification and Evidence.

If the provider outcome is unknown or requires reconciliation, the timeline says
so. It does not retry the action merely because the operator opened Explain.

## High-impact mutation blast radius

For each active ActionIntent the workspace displays the server-owned
ActionDefinition and ActionIntent fields needed to understand scope:

- risk class;
- provider/action identity;
- Resource IDs;
- required authority capabilities and level;
- reversible/verification requirements;
- authority, policy and security decisions;
- linked ApprovalRequest target/fingerprint;
- provider receipt and verification state.

Execution, retry, rollback and reconciliation remain in the canonical
ActionIntent APIs and their existing authorization/approval gates.

## Troubleshooting matrix

| Symptom | Canonical signal | Operator action |
| --- | --- | --- |
| Sync failed | BusinessDataSource last_error/status degraded | Inspect error and ProviderCapacity; do not move cursor manually. Reconcile from last durable cursor after cause is fixed. |
| Credential revoked/missing | credential_ref absent or provider reports credential/auth error | Update the SecretReference through secret/config administration, then bounded sync. Never paste credentials into business data. |
| Extension incompatible | linked extension lifecycle incompatible, with compatibility reason | Upgrade/replace the extension or codex-web according to compatibility policy. Do not force-enable it. |
| Extension quarantined | extension/source quarantined | Inspect extension health/audit reason, correct the package/configuration issue, then explicitly re-enable. |
| Stale cursor/source | no recent successful reconciliation, stale/out-of-order event counter increasing | Run bounded reconcile. Webhook events intentionally do not rewrite the durable cursor. |
| Partial KPI | KPI readiness partial, with readiness reasons | Inspect source CompanyFacts and resolve stale/missing/conflicting providers before using the KPI as current. |
| Provider outage | ProviderCapacity unavailable | Wait until retry_at or restore provider connectivity. Cursor remains on the last fully projected page. |
| Quota/rate limit | ProviderCapacity throttled/depleted | Respect retry_at. Repeated UI refreshes do not bypass capacity state. |
| Reconciliation conflict | source drift conflict count > 0 and fact diagnostics conflict=true | Inspect authoritative/secondary source provenance; correct source authority/mapping rather than overwriting canonical truth in the browser. |
| Unknown external action result | ActionIntent uncertain/requires_reconciliation + provider receipt outcome unknown | Use canonical ActionIntent reconciliation. Do not blind-retry a non-idempotent effect. |
| Approval expired/denied | ApprovalRequest expired/rejected | Create a new current request if still needed. Never reuse stale approval. |

## Synthetic fixture

The repository includes
`tests/fixtures/company_operations_synthetic.json` as sanitized, reproducible
operator-demo data. Names, IDs, provider records and monetary values are
synthetic and are not copied from a real customer or company.

The browser fixture under `tests/browser/company_operations_fixture.html`
exercises the same states: provider throttling, fact conflict, partial KPI,
pending approval and an unknown ActionIntent outcome requiring reconciliation.
