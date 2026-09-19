# Adopt Business Operations after engineering-only codex-web

This guide is for teams already using codex-web for repositories, Work Items,
agents and engineering automation that want to add company/business operations.

Do not jump directly from engineering automation to autonomous external business
mutations. Introduce read/sync company state first, then measured KPIs, then
advisory Executive roles, and only then narrowly authorized Actions.

## Phase 1 — inventory business systems

For each CRM, billing, support, analytics, campaign, product and finance system,
record:

- which provider records are authoritative;
- which fields are actually needed by Goals/Decisions/Executives;
- stable external IDs;
- a cross-source business entity key;
- classification/retention requirements;
- freshness requirements;
- credential owner/SecretReference;
- whether the integration is read-only or also needs a future ActionProvider.

Prefer the minimum sufficient set. Do not mirror entire provider datasets.

## Phase 2 — connect read/sync sources

Implement/register BusinessDataSource adapters and configure selected fact
mappings.

Promotion criteria:

- source cursors survive restart/outage;
- duplicate/out-of-order events are safe;
- provider conflicts are visible;
- ExternalRecordRef and CompanyFact provenance is inspectable;
- cross-tenant isolation tests pass;
- credentials remain outside normalized data.

At this stage, do not grant provider write Actions.

## Phase 3 — define business KPIs

Start with explicit versioned KPI definitions.

For each KPI, document:

- formula;
- included entity/fact scope;
- numerator/denominator semantics;
- window;
- unit/currency;
- freshness;
- thresholds;
- owner;
- source provenance.

Do not assume universal definitions for ARR, churn, conversion, active users,
gross margin or customer health.

Promotion criteria:

- a second operator can reproduce the result from CompanyFacts;
- formula revision makes old observations non-current;
- stale/conflicting facts make the KPI partial/missing;
- operating snapshots pin exact Metric revisions/observations.

## Phase 4 — bind Goals and Decisions

Link KPIs to canonical Goals and Decisions only after KPI semantics are stable.

Use explicit windows for time-bounded analysis. Capture target snapshots before
material decisions or review checkpoints.

Bindings add provenance; they do not grant authority.

## Phase 5 — enable advisory Executive roles

Suggested minimum-sufficient role contexts:

| Role | Typical structured inputs |
| --- | --- |
| CFO | finance/cost KPIs, governed revenue/cost facts, budget Goals/Decisions |
| CRO | pipeline/conversion/retention KPIs, customer/opportunity facts |
| CMO/Growth | campaign outcomes/cost/conversion KPIs and campaign facts |
| CPO | product adoption/usage KPIs, product-area facts and product Goals |
| Customer Success | support/SLA/health KPIs and customer/support relationships |
| COO | operations/capacity/cost state spanning explicitly relevant domains |
| Chief of Staff | bounded cross-functional context only when activation is cross-domain |

Executive output remains advisory until materialized as canonical Goal,
Decision, Work, ApprovalRequest or ActionIntent.

Promotion criteria:

- irrelevant roles are suppressed deterministically;
- every material recommendation can cite exact fact/KPI provenance;
- stale/conflicted inputs remain uncertainty;
- classification ceilings are enforced before model context.

## Phase 6 — add external business Actions

Only after the read/reasoning path is reliable should you add mutation providers.

Examples:

- CRM record update;
- customer communication;
- subscription/admin change;
- campaign change;
- finance/ops action.

Each must go through ActionProvider/ActionIntent.

For high-impact actions require:

- explicit Resource scope;
- Role authority;
- ApprovalRequest where policy requires it;
- budgets/entitlements;
- classification/security policy;
- provider receipt;
- verification/Evidence;
- reconciliation for unknown outcomes.

BusinessDataSource is never the write path.

## Phase 7 — production qualification

Before broader autonomy, exercise intentionally failing cases:

- stale source;
- conflicting providers;
- revoked credential;
- incompatible/quarantined extension;
- missing KPI input;
- provider outage;
- quota/rate limit;
- denied authority;
- expired approval;
- unknown provider outcome.

Operators should be able to diagnose every case from canonical state without a
model call.

## Rollback posture

If Business Operations adoption causes operational risk:

1. pause/quarantine affected BusinessDataSources;
2. pause relevant schedules/autonomy scopes;
3. keep canonical history/provenance intact;
4. disable or constrain ActionProvider capabilities independently;
5. correct configuration/source authority;
6. reconcile read state;
7. re-enable advisory reasoning before re-enabling external Actions.

Do not delete history merely to make the operating view look clean.
