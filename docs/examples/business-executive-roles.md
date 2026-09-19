# Role-specific governed Business Operations examples

All examples below use synthetic canonical objects and deliberately bounded
context.

## CFO — margin/cost review

Inputs:

- current finance/cost BusinessKPI items;
- exact KPI/Metric revisions and observation IDs;
- governed revenue/direct-cost CompanyFacts;
- budget Goal and related Decision;
- Evidence permitted by classification.

Expected output:

- explain variance against the configured threshold;
- identify which source facts contributed;
- state missing/stale/partial inputs;
- recommend a Decision or Goal revision.

Not allowed:

- recomputing known KPI arithmetic in the model;
- writing to billing/accounting provider directly;
- treating stale facts as current.

## CRO — pipeline and conversion

Inputs:

- revenue-domain KPIs only;
- selected opportunity/customer entities;
- pipeline/conversion fact provenance;
- active sales Goal/Decision bindings.

Expected output:

- reason over current pipeline/conversion state;
- cite exact KPI snapshot/fact sources;
- propose a Decision or Work plan.

A CRM update still requires ActionIntent + authorized CRM ActionProvider.

## CMO / Growth — campaign performance

Inputs:

- marketing KPI items and campaign entities;
- configured campaign cost/outcome facts;
- explicit attribution semantics from the KPI definition;
- campaign Goal/Decision references.

The Executive does not invent an attribution model when the source definition is
missing.

## CPO — product adoption

Inputs:

- product/adoption KPIs;
- relevant product/product-area entities;
- current product Goal/Decision;
- Evidence/memory allowed by governance.

A missing active-user definition is a configuration gap, not something for the
model to infer.

## Customer Success — support and health

Inputs:

- support responsiveness/customer-health KPIs;
- selected customer/support relationships;
- current source freshness/conflicts;
- service Goal/Decision bindings.

Customer outreach is an external side effect and must use an approved
ActionIntent/provider capability.

## COO / Chief of Staff — cross-functional event

Activate cross-functional roles only when the event/Goal/Decision spans multiple
domains.

For example, a company-wide cost-reduction Decision may retrieve:

- CFO cloud/service cost;
- CPO product usage;
- Customer Success support load;
- exact Goal/Decision references.

Routine single-domain events should not invoke every Executive role.

## Provenance checklist for every material recommendation

A recommendation should be traceable to:

1. activation reason / selected role;
2. exact BusinessEntity/CompanyFact references;
3. CompanyFact conflict/freshness state;
4. KPI ID + KPI revision;
5. Metric ID + Metric revision;
6. MetricObservation / MetricSnapshot IDs;
7. Goal / Decision references;
8. Evidence/memory inputs;
9. authority/approval state if converted to an ActionIntent;
10. provider receipt and verification if an external effect occurs.
