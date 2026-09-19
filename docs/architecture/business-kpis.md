# Business KPI catalogs and company operating snapshots

## Purpose

Business KPIs extend the existing M8 Metric domain. They do not create a second
metric engine.

A BusinessKpiDefinition adds explicit business semantics on top of one canonical
MetricDefinition:

- domain and optional code-owned template guidance;
- CompanyFact operand mappings;
- deterministic aggregation per operand;
- a safe arithmetic expression tree;
- currency/unit/window/freshness policy;
- target/threshold semantics;
- exact source-fact attribution.

The computed value is ingested as a normal MetricObservation. Exact operating
snapshots are persisted as MetricSnapshots and can therefore be reused directly
by Goals, Decisions, audit and Executive roles.

## KPI templates are guidance, not universal formulas

codex-web includes code-owned template metadata for common business measures such
as:

- recurring revenue;
- churn and retention;
- pipeline;
- conversion;
- support responsiveness;
- customer health;
- product adoption;
- service/cloud cost;
- gross margin;
- campaign performance.

Templates provide names, domains, default units/directions and formula guidance.
They do not contain an implicit SaaS formula.

Each tenant defines its own explicit operands, source fact keys, aggregation,
formula, currency, window and freshness policy.

For example, gross margin may be configured as:

```text
((revenue - direct_cost) / revenue) * 100
```

but codex-web does not assume those inputs or accounting semantics universally.

## Safe deterministic expression model

Business KPI formulas support only:

- operand references;
- numeric constants;
- addition;
- subtraction;
- multiplication;
- division.

There is no Python/JavaScript eval, generated code or LLM arithmetic.

Division by zero, unavailable required operands and non-numeric facts fail
visibly.

## Operand selection

Each operand identifies:

- a CompanyFact key;
- an explicit set of BusinessEntity IDs, one or more entity types, or the
  bounded current workspace entity set;
- SUM, AVERAGE, MINIMUM, MAXIMUM, COUNT or LAST aggregation;
- a BLOCK or PARTIAL missing-data policy.

The evaluator calls canonical CompanyFact resolution for every selected entity.
It retains:

- selected fact IDs;
- provider conflict fact IDs;
- stale fact IDs;
- revoked-source fact IDs;
- missing entity IDs;
- oldest/newest selected timestamps.

No provider prose is substituted for unavailable structured facts.

## Freshness and partial state

A KPI cannot silently become current when its inputs are not current.

- a blocked required stale operand produces no number and KPI freshness STALE;
- another unavailable blocked operand produces MISSING;
- conflicting, stale-competing or revoked-competing provider state marks the
  computed MetricObservation PARTIAL;
- PARTIAL is carried into exact Metric snapshots and company operating views.

Trend arithmetic is not published when the current observation or its previous
baseline is partial.

## KPI and Metric revision lockstep

Every BusinessKpiDefinition revision forces a new canonical MetricDefinition
revision, including formula-only changes.

The Metric source requirement is revision-pinned:

```text
business-kpi:<kpi-id>:r<revision>
```

and the resulting MetricObservation uses that same source identity.

This preserves exact attribution between:

- Business KPI revision;
- Metric definition revision;
- formula;
- CompanyFact inputs;
- Metric observation.

## Observation attribution

Each computed observation stores BusinessKpiObservationAttribution containing:

- KPI ID/revision;
- Metric ID/revision;
- MetricObservation ID;
- full operand attribution;
- exact selected CompanyFact IDs;
- deterministic formula fingerprint.

The fingerprint is calculated from the KPI revision, expression, operand
attribution and requested window. Re-evaluating identical inputs is idempotent.

## Targets, variance and trends

Target evaluation supports EQ, GTE and LTE deterministically.

The operating result records:

- pass/fail;
- absolute variance;
- percentage variance where the target is non-zero.

Trend compares against the previous complete canonical MetricObservation and
records absolute/percentage delta. A partial current or prior observation
suppresses numeric trend deltas.

## Exact Goal bindings

Binding a KPI to a Goal:

1. deterministically evaluates the KPI;
2. refuses stale/missing state and refuses partial unless explicitly permitted;
3. persists an exact MetricSnapshot containing the single KPI observation;
4. adds/replaces a canonical GoalSuccessCriterion referencing:
   - Metric ID;
   - exact MetricSnapshot ID;
   - configured window;
   - target/operator/unit.

The Goal therefore retains the exact measured state used for its success
criterion.

## Exact Decision bindings

Binding a KPI to a Decision follows the same freshness gate and exact-snapshot
rule.

The canonical Decision evidence stores the MetricSnapshot ID and resolves into:

- Metric revision;
- freshness;
- observed value;
- unit;
- exact observation IDs;
- window.

BusinessKpi does not add a separate Decision-evidence format.

## Company operating snapshot

BusinessKpiOperatingSnapshot evaluates all configured KPIs at one capture time
and records minimum-sufficient structured operating state.

Each item contains:

- KPI ID/revision/domain;
- Metric ID/revision;
- exact MetricSnapshot/observation IDs when available;
- value/unit/freshness;
- target and trend state;
- source CompanyFact IDs;
- source ExternalRecordRef IDs;
- bound Goal IDs;
- bound Decision IDs;
- freshness/conflict reasons.

Executive roles can consume this snapshot instead of receiving a prose dump or
recomputing arithmetic in the model.

## UI

The Company KPIs workspace shows the latest persisted operating snapshot with
freshness badges, target/variance, trend, formula and exact provenance.

Operators can drill directly into canonical Metric, Goal and Decision
workspaces, while CompanyFact/ExternalRecordRef links retain source provenance.

Capturing a current operating snapshot is an explicit mutation because it
creates Metric observations/snapshots. Reading the latest persisted operating
snapshot is side-effect free.

## Authority

Authenticated tenant users may read definitions/templates and persisted
operating snapshots.

Creating/revising/evaluating KPIs, capturing operating snapshots and binding
Goals/Decisions requires administrator + MFA or a service identity with
`business-kpi:admin`.

This authority affects codex-web measurement state only. It grants no external
provider mutation authority.

## Failure semantics

- missing required CompanyFact -> MISSING, no MetricObservation;
- stale required CompanyFact -> STALE, no MetricObservation;
- provider conflict -> PARTIAL observation;
- revoked/stale competing source -> PARTIAL observation;
- non-numeric mapped fact -> validation failure;
- division by zero -> validation failure;
- unknown formula operand -> definition validation failure;
- stale/missing binding attempt -> rejected;
- partial binding attempt -> rejected unless explicitly allowed;
- cross-tenant KPI lookup -> not found.

These are deliberate deterministic states.
