# Governed Executive business context

## Purpose

Milestone 13 extends the existing M9 Executive Management pipeline so
non-engineering Executive roles reason over canonical company state rather than
free-form business facts copied into chat.

The integration reuses:

- BusinessEntity, ExternalRecordRef and CompanyFact;
- BusinessKPIDefinition / CompanyOperatingView;
- Goal and Decision;
- Organizational Memory;
- Evidence;
- Role Authority;
- ApprovalRequest / DecisionWork / ActionIntent / ActionProvider.

It does not create a second Executive runtime or a direct business-provider
mutation path.

## Business observable-information contracts

Executive roles can declare three additional observable object types:

- business_entity;
- business_fact;
- business_kpi.

They also declare business_domains and a maximum data classification allowed in
model context.

The seeded functional domains are intentionally specialist-oriented:

| Role | Primary business domains |
| --- | --- |
| CFO | finance, cost |
| CRO | revenue |
| CMO / Growth | marketing |
| CPO | product |
| Customer Success | customer_success |
| COO | operations |
| Chief of Staff | cross-functional union/fallback |

Chief of Staff does not join a routine single-domain activation merely because
it can observe all domains. Cross-functional scope or its own event/keyword
relevance is required.

## Deterministic role activation

Executive selection remains deterministic and bounded.

Primary relevance is produced by:

1. explicit requested role IDs;
2. canonical event subscription;
3. role keywords;
4. canonical business domains.

Goal/Decision/Work/Evidence references are context bonuses only after a role has
a primary relevance signal. Merely being able to observe a Goal no longer makes
every Executive role relevant.

Business domains are derived from:

- explicit activation business_domains;
- explicit BusinessEntity types;
- explicit Business KPI definitions;
- Business KPI bindings on supplied Goal IDs;
- Business KPI bindings on supplied Decision IDs.

Selection records exact reasons such as business-domain:revenue. The activation
persists the resolved business KPI IDs/domains so replay does not need to infer
them from later mutable state.

## Minimum-sufficient retrieval

Business retrieval is bounded by the selected role context limits and the
global Executive reference cap.

Explicit BusinessEntity references can contribute:

- identity/type/name;
- canonical links;
- field provenance;
- current resolved CompanyFacts.

Business domains can contribute only matching Business KPIs. The Executive
pipeline uses BusinessKPIService.operating_items for exact bounded KPI IDs; it
does not evaluate the entire company catalog by default.

No BusinessDataSource provider payload is sent to the model.

## Data governance before model context

Business entities and CompanyFacts must have canonical DataGovernance records.

Before a business object is serialized into Executive model context,
DataGovernanceService.filter_context checks:

- active lifecycle;
- credential/secret exclusion;
- deny_model_context;
- effective classification;
- the selected role classification ceiling.

A missing governance record fails closed.

When a KPI has a value, its current BusinessKPIRefreshResult must provide exact
source-fact provenance. Those CompanyFact governance records are checked before
the KPI value enters model context.

If any required source fact is denied, the KPI value is withheld and a
non-sensitive denial record is persisted in ExecutiveCanonicalContext.

Per-role filtering happens again before consultation, so one role with a higher
classification ceiling cannot leak that data to another consulted role.

## Stale, partial and conflicting state

The Executive service does not repair or reinterpret measured state.

BusinessFact context retains:

- freshness;
- conflict flag and conflicting fact IDs;
- stale/revoked-source IDs;
- selected CompanyFact provenance when permitted.

Business KPI context retains:

- KPI revision;
- Metric ID/revision;
- formula/window/currency;
- Metric currentness/readiness;
- exact observation IDs;
- current refresh source fact IDs and findings.

Stale, partial, missing or conflicting business state is passed as uncertainty,
not as a current fact. The role system prompt explicitly prohibits converting
it into apparently current measured state.

## Exact recommendation provenance

Business model-context rows receive stable citation markers:

- [business-entity:<id>]
- [company-fact:<id>]
- [company-fact-resolution:<entity>:<key>]
- [business-kpi:<id>@r<revision>/metric:<id>@r<revision>]

ExecutiveRoleOutput includes context_refs. When a role is given governed
business context, it must record at least one supplied marker and may not cite a
marker absent from its filtered canonical context.

The validated output is stored in the Executive consultation, so the activation
records both:

- why the role was selected; and
- which exact business facts/KPIs the role says materially informed its output.

Canonical context itself remains persisted on the activation for independent
audit/replay.

## External effects remain canonical actions

Executive roles remain advisory. Their authority contract has
external_side_effects=false.

The role prompt specifically forbids direct:

- CRM updates;
- customer communications;
- subscription/admin changes;
- campaign changes;
- finance/operations provider actions;
- similar external business effects.

An Executive output can only propose existing canonical proposal kinds:

- Goal;
- Decision;
- Work;
- Escalation.

Materialization still passes Role Authority. Work proposals still flow through
DecisionWork and therefore the existing approved-Decision / ActionIntent /
ActionProvider boundaries. The Executive service receives no BusinessDataSource
write method and no direct external provider client.

A denied authority evaluation leaves the proposal advisory and persists the
denial reasons.

## Business events

BusinessDataSource already normalizes provider changes into
business_data.event canonical events. Event-triggered Executive activations may
supply normalized business-domain/entity/KPI metadata to the same deterministic
selection path.

The Executive service does not subscribe every business role to every
business_data.event and does not automatically invoke models from provider
webhooks. Event-driven reasoning must still pass the existing canonical
event/autonomy scheduling and reasoning gates. This prevents routine business
events from invoking the full Executive catalog.

## Budgets, entitlements and bounded consultation

The existing Executive reasoning budget remains authoritative for:

- input/output tokens;
- model call count;
- cost.

ModelGateway continues to enforce provider/model entitlements and routing.
Business context does not bypass those controls.

Multi-role consultation remains bounded by the role catalog and activation
max_roles. Specialist domain routing reduces unnecessary calls before model
invocation.

## Failure semantics

- missing BusinessEntity/KPI -> activation context error;
- cross-tenant reference -> unavailable/not found;
- missing governance metadata -> context denied;
- SECRET / deny_model_context source -> context denied;
- stale/partial/conflicting KPI -> explicitly non-current;
- fabricated model context_ref -> consultation rejected;
- insufficient model-call budget -> activation rejected before invocation;
- Role Authority denial -> proposal stays advisory;
- direct external side effect -> unsupported by Executive proposal contract.

These are fail-closed states.
