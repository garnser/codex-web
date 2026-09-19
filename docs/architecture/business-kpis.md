# Business KPI catalogs and company operating views

## Purpose

Business KPI catalogs are a configuration/projection layer over the
canonical Metric/KPI primitive. They do not introduce a second metric engine.

Business KPI configuration answers a business question:

- which canonical CompanyFacts participate;
- which BusinessEntity population is in scope;
- which deterministic formula combines those facts;
- what currency/unit/window/freshness policy applies;
- what target thresholds matter;
- which Goals and Decisions consume the result.

The resulting value is ingested as a normal versioned MetricObservation.
Aggregation, currentness, snapshots and Decision metric evidence continue to use
MetricService.

## No universal SaaS formula

codex-web does not assume that ARR, churn, conversion, active usage, gross
margin, customer health or campaign performance have one universally correct
formula.

Every active BusinessKPIDefinition explicitly versions:

- fact terms and entity scope;
- term aggregation;
- numerator/denominator aliases where applicable;
- formula kind;
- scale;
- observation window;
- freshness policy;
- currency/unit;
- threshold definitions;
- canonical Metric ID/revision.

Changing formula semantics creates both a new Business KPI revision and a new
Metric definition revision. Old-revision MetricObservations do not become
current observations under the revised semantics.

## Starter packs

The API exposes code-owned starter metadata for:

- CFO: recurring revenue, gross-margin inputs, cloud/service cost;
- CRO: pipeline, conversion, retention/churn;
- CMO: campaign performance;
- CPO: product adoption;
- Customer Success: support responsiveness and customer health.

These templates are not active KPI definitions and do not contain hidden
formulas. They state candidate measures, supported deterministic formula shapes
and the inputs that a tenant must explicitly define.

## Formula terms

A BusinessKPIFactTerm selects:

- a CompanyFact key;
- either a BusinessEntity type or explicit BusinessEntity IDs;
- a deterministic aggregation: sum, average, minimum, maximum or count.

Only current CompanyFact resolution is used. Stale, missing or revoked-source
facts do not silently become current KPI inputs. Provider conflicts remain
visible and make the resulting observation partial.

## Formula kinds

Supported deterministic arithmetic is deliberately small and inspectable:

- aggregate: selected term × scale;
- ratio: left / right × scale;
- difference: (left - right) × scale;
- percent_change: ((left - right) / abs(right)) × 100 × scale.

Zero denominators/baselines fail to a partial refresh with no fabricated value.
No language model performs arithmetic, thresholding or trend calculation.

## Projection into MetricService

A refresh:

1. resolves every configured fact term at one evaluation timestamp;
2. records fact IDs, source ExternalRecordRef IDs and findings;
3. computes the explicit formula deterministically;
4. writes one MetricObservation using source business-kpi:<KPI ID>;
5. marks that observation partial when any required input is stale, missing,
   conflicting or non-numeric;
6. stores a refresh record linking KPI revision, Metric revision and exact
   source fact provenance.

Observation idempotency derives from KPI revision, Metric revision, source fact
IDs and result value.

## Metric revision currentness

MetricService evaluates current state using observations produced under the
current Metric definition revision only.

This is important for business KPI changes: changing a formula/window/threshold
cannot make an old observation look current under new semantics. The revised KPI
remains missing until a new observation is produced.

Historical Metric observations and immutable snapshots remain available for
audit.

## Current company operating view

The operating view composes current Metric evaluations and adds business context:

- KPI and Metric revisions;
- value/unit/currency;
- configured window;
- freshness/readiness;
- exact observation IDs;
- same-revision trend delta and percent;
- deterministic threshold state and variance;
- source fact keys and ExternalRecordRefs;
- bound Goal IDs and Decision IDs.

A KPI is CURRENT only when the current Metric evaluation is fresh and the latest
business-KPI refresh is complete. Partial/missing/stale input therefore cannot
silently drive an apparently current executive operating view.

Overall company operating state is current only when every configured KPI is
current.

## Trend and thresholds

Trend compares the newest two observations from the same Metric definition
revision. Cross-revision comparisons are intentionally excluded.

Thresholds use the canonical Metric definition threshold contract. Their state is:

- met;
- not_met;
- unknown.

Stale, missing or partial KPIs produce unknown threshold state rather than
presenting an old comparison as a current business result.

Numeric variance is observed value minus target value.

## Goal and Decision bindings

A BusinessKPITargetBinding links one KPI to an existing canonical Goal or
Decision and optionally records an explicit analysis window/purpose.

Bindings do not create authority. They are provenance/context links.

CompanyOperatingSnapshot captures one immutable MetricSnapshot per KPI plus:

- Business KPI revision;
- Metric definition revision;
- MetricSnapshot ID;
- exact MetricObservation IDs;
- value/unit/freshness/window;
- bound Goal IDs;
- bound Decision IDs.

Each Goal/Decision binding also receives a BusinessKPITargetSnapshot. When the
binding declares its own window, that target snapshot captures a separate
MetricSnapshot using exactly that window; otherwise it can reuse the company
snapshot. The target snapshot records binding ID, target ID/type, KPI revision,
Metric revision, MetricSnapshot ID and exact observation IDs.

The target-snapshot API can therefore answer which exact measured state and
window were available for a Goal or Decision without reconstructing history
from prose.

Existing Decision metric-snapshot evidence remains the canonical mechanism when
a Decision formally cites a measured value.

## Source provenance

Business KPI source provenance composes the business-context and business-data-source boundaries:

BusinessDataSource
-> ExternalRecordRef
-> CompanyFact
-> BusinessKPIRefreshResult
-> MetricObservation
-> MetricSnapshot / CompanyOperatingSnapshot
-> Goal / Decision / Executive context

Provider data and canonical measured state remain visibly distinct throughout
that chain.

## UI

The Company operating view shows:

- current/not-current overall status;
- value and same-revision trend;
- freshness/readiness;
- threshold state and variance;
- KPI/Metric revisions;
- observation IDs;
- source ExternalRecordRefs;
- Goal/Decision bindings.

Metric links open the existing Metric explorer. Source/Goal/Decision links point
to their canonical API resources.

The UI reports canonical calculated state and does not implement formulas or
currentness independently.

## Executive integration

Executive integration consumes CompanyOperatingView and CompanyOperatingSnapshot as
minimum-sufficient structured inputs for CFO/CRO/CMO/CPO/Customer Success
Executive roles.

Executive reasoning must receive exact KPI/Metric revisions, currentness and
observation provenance. It must not recompute known arithmetic or replace stale
state with prose guesses.
