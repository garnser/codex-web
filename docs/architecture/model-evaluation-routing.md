# Model evaluation, qualification, and routing guidance

## Status

Architecture policy for evaluating concrete models and maintaining workload-to-model mappings over time.

This document extends the canonical [Model Gateway](model-gateway.md), [Capability-driven agent routing](agent-routing.md), [Token Efficiency Ruleset](token-efficiency-rules.md), and [Autonomous evaluation/replay](autonomy-evaluation-replay.md) contracts.

The central rule is:

> **Codex-web routes by stable workload capabilities and policy. Concrete provider/model mappings are versioned, evidence-backed registry data that are expected to change over time.**

No Executive role, business domain, Work Item type, UI page, or orchestration path should hard-code a provider-specific model name.

The ordering is mandatory: **first determine what can actually execute under the
current provider/runtime configuration; then select the best qualified model for
the workload from that candidate set.** Catalog advertisement, benchmark presence,
or a historical mapping does not establish executability.

The active candidate universe is the intersection of enabled and healthy runtimes,
usable authentication, tenant policy, fresh required catalogs, exact runtime/access
catalog membership, capabilities, and provider-scoped workload qualification.
Ranking occurs only after this intersection. Adding or disabling a provider changes
the candidate-set revision without changing domain or orchestration code.

## Why this policy exists

Codex-web spans materially different kinds of reasoning:

- lightweight classification, extraction, summarization, and context compression;
- implementation, debugging, testing, and code review;
- repository-wide and multi-repository engineering;
- architecture and ADR reasoning;
- Goal decomposition and roadmap planning;
- Executive and Board analysis;
- KPI and Company Operations interpretation;
- incident, operations, and security reasoning;
- research and evidence synthesis;
- organizational-memory extraction and synthesis.

The model that is most cost-effective for one workload may be a poor choice for another. Model catalogs, prices, context windows, tool support, provider availability, benchmark performance, and billing semantics also change continuously.

Therefore the routing mapping must be treated as a governed, revisable operational definition rather than permanent source-code truth.

## Non-LLM boundary

Before evaluating a model for any function, determine whether a model should be used at all.

The following remain deterministic application responsibilities:

- permissions, authority, RBAC, tenant/resource scope;
- ApprovalRequest state and approval decisions;
- policy enforcement;
- budget/quota enforcement;
- KPI/formula calculation;
- provider reconciliation;
- dependency/readiness calculation;
- scheduling, retry, dedupe, idempotency, and lifecycle transitions;
- canonical usage accounting;
- cryptographic/security invariants;
- known structured transformations that normal code can perform reliably.

Models may interpret, recommend, explain, plan, critique, synthesize, or propose structured changes. They do not become the source of truth for deterministic controls.

## Stable workload classes

Concrete model mappings should be expressed through stable workload/capability classes. The initial taxonomy should support at least:

| Workload class | Typical codex-web functions | Expected characteristics |
| --- | --- | --- |
| `lightweight` | intent classification, summarization, context compression, helper text, memory extraction | low cost, low latency, reliable structured output |
| `primary-coding` | implementation, tests, ordinary debugging, routine repository work | strong coding/tool performance, good cost per completed task |
| `coding-deep` | complex debugging, multi-file/multi-repo work, difficult code review | stronger repository reasoning and tool reliability |
| `high-reasoning` | Work Item decomposition, difficult review, incident analysis, complex engineering | strong reasoning with bounded cost |
| `architecture` | architecture reviews, ADRs, platform design, significant technical trade-offs | deep long-horizon reasoning, constraint retention, strong critique |
| `strategic` | Executive/Board analysis, business strategy, roadmaps, major cross-domain decisions | professional/strategy reasoning, trade-off quality, calibrated uncertainty |
| `operator` | complex operations plans, incident strategy, action planning, automation-heavy reasoning | strong tool/agent planning and execution reliability |
| `research` | market/competitor/external evidence gathering | retrieval/search strength, source grounding, evidence traceability |
| `synthesis` | board briefs, executive reports, organizational-memory synthesis | long-context synthesis, factual retention, concise decision-ready output |
| `critic` | independent review of architecture, strategic decisions, high-risk plans | different model family where practical, adversarial error finding |

A model may qualify for multiple workload classes.

The taxonomy is intentionally about the cognitive job, not the UI surface. One page may invoke several workload classes at different steps.

## Functional routing baseline

The canonical routing definitions should be able to express mappings equivalent to the following baseline:

| codex-web function | Default workload class | Escalation/review class |
| --- | --- | --- |
| Request classification / intent detection | `lightweight` | none |
| Model-routing classification | deterministic first; otherwise `lightweight` | none |
| Simple chat / user assistance | `lightweight` | `high-reasoning` |
| Work Item decomposition | `high-reasoning` | `strategic` |
| Implementation / coding | `primary-coding` | `coding-deep` then `high-reasoning` |
| Complex debugging / multi-repo engineering | `coding-deep` | `high-reasoning` |
| Code / PR review | `high-reasoning` | `critic` for high-risk changes |
| Test generation | `primary-coding` | `coding-deep` |
| Architecture / ADR generation | `architecture` | `critic` |
| Major platform architecture | `architecture` | independent `critic` from another model family |
| Goal creation / refinement | `high-reasoning` | `strategic` |
| Goal to strategy / Work Item decomposition | `high-reasoning` | `strategic` |
| Roadmap planning / prioritization | `strategic` | `critic` |
| First-class Decision deliberation | `strategic` | independent `critic` |
| CEO / Board strategy | `strategic` | `critic` |
| CFO interpretation / scenarios | `strategic` | `critic` for consequential decisions |
| CTO technical strategy | `architecture` or `strategic` | `critic` |
| COO operations reasoning | `operator` | `strategic` or `critic` |
| KPI interpretation | `high-reasoning` | `strategic` |
| KPI calculation | deterministic | none |
| Business-data reconciliation | deterministic | model may explain anomalies only |
| Company Operations analysis | `high-reasoning` | `strategic` |
| Business opportunity / strategic analysis | `strategic` | `critic` |
| Market / competitor research | `research` | `synthesis` / `strategic` |
| Executive / board report | `synthesis` | `strategic` when judgment is required |
| Organizational-memory extraction | `lightweight` | `high-reasoning` |
| Organizational-memory synthesis | `synthesis` | `strategic` |
| Context compression | `lightweight` | none |
| Attention / Inbox summarization | `lightweight` | `high-reasoning` for ambiguity |
| Incident triage | `high-reasoning` | `operator` |
| Major-incident strategy | `operator` | independent `critic` |
| Security/threat reasoning | `high-reasoning` | `critic` / `strategic` |
| Automation definition from natural language | `lightweight` | `high-reasoning` |
| Automation triggering/scheduling | deterministic | none |
| Approval decision | human/policy | model may summarize evidence only |
| Agent Profile / Team design | `high-reasoning` | `strategic` |
| Documentation / release notes | `lightweight` or `synthesis` | none |

This table defines the intended *class* mapping. Concrete model assignments belong in the Model Gateway / Definition Registry and are versioned independently.

The initial concrete recommendations are bootstrapped as the published Definition
Registry record `model-routing.initial-functional-matrix` of kind
`model-routing-baseline`. Its `initial-functional-matrix-2026-09-26` evaluation
revision and evaluation timestamp are data, and the record explicitly declares that
it is replaceable. This baseline is discovery input only: it does not make a provider
catalog entry eligible for production. An operator must register the provider/model,
record passing replay evidence against the workload's evaluation-profile revision,
and publish an effective Model Gateway mapping revision.

Effective mappings and lifecycle changes live in Model Gateway state. Qualification,
canary, active, restricted, and retired transitions append immutable revisions.
Publishing or rolling back a mapping also appends a revision; it never rewrites the
invocation provenance retained against an older revision. Routing applies the mapped
order only after tenant policy, capability, residency, compliance, context, pricing,
catalog freshness, provider capacity, and current qualification checks pass.

## Executive-team diversity

For consequential decisions, do not create an illusion of independent Executive debate by assigning every role to the same concrete model and prompt family.

When policy allows multiple participants:

1. select only materially relevant roles;
2. prefer independent analysis in parallel;
3. use at most the bounded participant/round limits from the Token Efficiency Ruleset;
4. for high-impact architecture or strategy, prefer at least one critic using a meaningfully different model family/provider when an eligible alternative exists;
5. perform one synthesis step;
6. record the exact model/provider/prompt/definition revision for every participant.

Model diversity is not automatically better; it is useful when the cost is justified by decision impact and when it gives genuinely independent failure modes.

## Model qualification lifecycle

A newly discovered model is **available**, not automatically **qualified**.

Use the following lifecycle:

```text
discovered
    |
    v
metadata-valid
    |
    v
candidate
    |
    +--> offline qualification
    |
    +--> shadow / paired evaluation
    |
    v
qualified for workload class(es)
    |
    v
canary
    |
    v
active
    |
    +--> degraded / restricted
    |
    +--> retired
```

A provider-recommended alias may be used only where policy explicitly allows provider-managed selection. It must still satisfy capability, cost, residency, health, authentication, runtime/access-source catalog, and evaluation requirements appropriate to that workload.

## Step 1: collect authoritative model metadata

Prefer provider/runtime discovery over hard-coded catalogs.

Capture, where available:

- provider/runtime and upstream provider family;
- concrete model ID, version, alias, and release/revision date;
- lifecycle/availability status;
- context and maximum output limits;
- tool/function calling support;
- structured-output support;
- modality support;
- reasoning-effort controls;
- streaming/session capabilities;
- cache behavior where relevant;
- exact/partial usage reporting;
- input/output/cache/reasoning pricing;
- rate limits and quota/billing semantics;
- data residency/compliance characteristics;
- known deprecation dates.

Never infer a capability merely from the model name.

## Step 2: hard eligibility gates

A model is ineligible for a workload before scoring if it fails any mandatory constraint, including:

- required context capacity;
- required tools/structured output;
- provider/runtime health;
- tenant/provider/model allowlists;
- residency/compliance policy;
- execution/runtime compatibility;
- maximum invocation/task budget;
- required usage/cost provenance;
- required modality;
- required safety or sandbox integration semantics.

Scoring never overrides a failed hard constraint.

## Step 3: evaluate on workload-specific evidence

Do not use one universal benchmark score.

Each workload class has its own evaluation profile. External benchmarks are useful discovery signals, but internal codex-web evaluations determine qualification.

### Lightweight

Measure:

- extraction/classification correctness;
- schema-valid structured output rate;
- summary factual retention;
- instruction-following;
- p50/p95 latency;
- cost per accepted result.

### Primary coding / coding-deep

Measure:

- task completion rate;
- tests passing after generated changes;
- regression rate;
- repository/tool execution success;
- number of retries/handoffs;
- review defect rate;
- context efficiency;
- latency and cost per accepted PR/work item.

Use coding-agent and terminal/repository benchmarks as external evidence, but prefer representative codex-web repository fixtures and historical replay.

### High reasoning / architecture

Evaluate with architecture and ADR fixtures containing competing constraints.

Score:

- constraint retention;
- identification of hidden assumptions;
- trade-off completeness;
- failure-mode coverage;
- migration/rollback quality;
- security/operability implications;
- consistency with existing architecture contracts;
- unsupported-fact/hallucination rate;
- reviewer preference using a fixed rubric.

Architecture evaluation should reward identifying what *not* to build, not merely producing more elaborate designs.

### Strategic / Executive

Use business/strategy fixtures that contain incomplete information, conflicting objectives, budgets, KPIs, uncertainty, and second-order effects.

Score:

- correct use of supplied facts/KPIs;
- separation of fact, assumption, and recommendation;
- option coverage;
- quality of trade-off analysis;
- risk and second-order-effect coverage;
- calibration under uncertainty;
- actionability;
- consistency with authority/policy boundaries;
- ability to request missing evidence rather than invent it;
- outcome quality on historical decisions where a fair replay baseline exists.

Professional/strategy benchmarks can be external signals, but must not replace internal Executive/Decision fixtures.

### Operator / incident

Measure:

- diagnosis quality;
- safe sequencing;
- rollback/containment planning;
- tool-plan validity;
- evidence requirements;
- rate of unsafe or unauthorized proposed actions;
- completion rate under injected failures;
- time/cost to verified recovery.

### Research

Measure:

- source quality;
- source coverage;
- citation correctness;
- freshness;
- distinction between evidence and inference;
- contradiction handling;
- synthesis accuracy.

### Synthesis / memory

Measure:

- factual retention;
- omission rate for decision-critical facts;
- provenance retention;
- compression ratio;
- contradiction preservation;
- ability to distinguish stale from current information.

### Critic

Measure how often the critic discovers material defects that the primary model missed:

- invalid assumptions;
- missing alternatives;
- policy/authority violations;
- security/operational risks;
- unsupported conclusions;
- hidden cost or migration consequences.

A critic that mostly restates the primary answer is not useful even if its general benchmark score is high.

Critic provenance records the requested and achieved level:
`different_provider_family`, `different_model_family_same_provider`,
`different_model_same_family`, `same_model_independent_run`, or `none`. Selection
prefers the strongest qualified level that is actually executable. A workload may
set a minimum and fail routing when it cannot be met. Same-provider and same-model
reviews are never described as full independence, and routing does not choose an
inferior unqualified model merely to create apparent diversity.

## Step 4: evaluate economics by outcome

Token price alone is not the optimization target.

Prefer metrics such as:

```text
cost per accepted Work Item
cost per successful PR
cost per verified incident resolution
cost per accepted architecture decision
cost per useful Executive decision
tokens per successful outcome
retries per successful outcome
latency per successful outcome
```

A model that costs more per token may be cheaper per completed task if it finishes reliably in fewer attempts.

Record both provider-reported cost and any derived estimate according to the canonical usage-accounting contract. Entitlement-backed ChatGPT/Codex access retains authoritative allowance and token telemetry but never converts comparison pricing into a realized API charge.

## Step 5: build a workload scorecard

For each workload class, define a versioned evaluation profile with explicit weights.

Illustrative structure:

```yaml
workload: architecture
revision: 3
hard_requirements:
  min_context_tokens: 128000
  structured_output: true
weights:
  internal_quality: 0.40
  constraint_retention: 0.15
  critique_failure_modes: 0.15
  verified_task_success: 0.10
  cost_per_success: 0.10
  latency: 0.05
  external_evidence: 0.05
```

Weights are definitions/data, not scattered constants in domain code.

Do not combine unrelated workloads into one global "best model" score.

## Step 6: shadow and paired evaluation

Before replacing an established mapping for important workloads:

- run the candidate against a representative sample of historical/replayable tasks;
- where possible, run candidate and incumbent from the same canonical input/checkpoint;
- prevent shadow runs from causing external side effects;
- compare outputs using deterministic validation first;
- use blinded human or rubric-based review for subjective architecture/strategy quality;
- measure actual tokens, provider cost, latency, failures, retries, and outcome quality;
- retain exact model, prompt, Definition, fixture, and evaluation revisions.

For high-impact Executive or architecture workloads, a small number of high-quality reviewed fixtures is preferable to a large low-signal synthetic leaderboard.

## Step 7: promotion rules

A candidate may be promoted when it:

1. passes all hard constraints;
2. meets the minimum quality threshold for the workload;
3. does not introduce an unacceptable regression on safety, correctness, or policy compliance;
4. demonstrates acceptable outcome economics;
5. completes a bounded canary period when production evidence is required.

Promotion should create a new mapping/definition revision. Do not overwrite historical routing provenance.

For low-risk workloads, cost/latency improvement may justify promotion at equivalent quality.

For architecture, security, Executive, and other high-impact reasoning, quality and reliability thresholds dominate small cost savings.

## Step 8: rollback and retirement

Every mapping change must be reversible.

Rollback triggers may include:

- provider/model degradation;
- quality regression;
- tool/structured-output regressions;
- pricing changes that violate budget policy;
- context/capability changes;
- deprecation;
- unacceptable latency;
- observed increase in retries/failures;
- policy/residency/compliance changes.

Historical Runs/Decisions retain the exact model/mapping/evaluation revisions used at execution time.

Retired models remain visible in historical provenance but are ineligible for new routing.

## Evaluation cadence and triggers

Do not run expensive benchmark suites continuously.

Re-evaluate when one of these events occurs:

- a provider exposes a materially new model;
- a model receives a significant version/revision update;
- pricing changes materially;
- context/tool/structured-output capabilities change;
- a provider deprecates or removes a model;
- production outcome metrics regress;
- a workload's requirements change;
- an operator explicitly requests requalification.

A lightweight metadata refresh may occur more frequently and consume zero LLM tokens.

## External benchmark policy

External leaderboards and vendor claims are evidence, not routing truth.

When using them:

- record source and retrieval date;
- prefer task-relevant benchmarks over aggregate intelligence scores;
- distinguish vendor-reported from independent results;
- note benchmark version and inference/reasoning settings;
- compare cost assumptions on the same basis;
- do not mix results from materially different model variants as if they were identical;
- use external evidence to select candidates, then confirm with internal qualification.

## Production telemetry feedback

Qualification should eventually consume real codex-web outcome telemetry, including:

- successful/failed Work Items by workload/model;
- accepted/rejected PR or review outcomes;
- retries and escalations;
- human correction rate;
- decision acceptance/revision rate;
- incident recovery outcome;
- token/cost usage;
- latency;
- context-limit failures;
- structured-output/tool-call failures.

Production telemetry must be interpreted carefully: workload mix and task difficulty can change. Do not automatically conclude that one model is better solely from unadjusted aggregate success rates.

## Mapping representation

The effective mapping should live in canonical versioned definitions/registry state and support:

- workload class;
- provider/runtime scope;
- ordered eligible concrete models;
- primary/default candidate;
- escalation candidate(s);
- independent critic candidate(s);
- required capabilities;
- maximum cost/task or invocation;
- reasoning-effort preference;
- fallback policy;
- qualification/evaluation revision;
- effective date;
- operator/automation provenance.

Domain code requests the workload/capabilities. The routing layer resolves the current eligible concrete model.

## Initial model-seeding principle

Bootstrap configuration may ship with an initial set of model mappings based on evidence available at release time, but those mappings are defaults, not architecture constants.

A bootstrap seed must:

- be labeled with an evaluation date/revision;
- be replaceable through canonical configuration/definitions;
- not assume all providers expose the same model catalog;
- not make a provider/model permanently synonymous with a workload class;
- be safe when a configured model disappears.

## Change review checklist

Any PR or configuration change that adds/promotes/replaces a concrete model mapping should answer:

1. Which workload class(es) does this affect?
2. Which hard capability/policy requirements were checked?
3. Which exact provider/model/version was evaluated?
4. Which internal fixture/evaluation revision was used?
5. Which relevant external evidence was considered and when?
6. What are quality, completion, retry, latency, token, and cost-per-outcome results?
7. What incumbent was compared?
8. Is the difference statistically/practically meaningful for the sample size available?
9. What is the canary scope?
10. What is the rollback condition and previous known-good mapping?
11. Does the change preserve deterministic/human authority boundaries?
12. Is exact routing provenance retained?

## Core principle

> **Choose models for the work they demonstrably perform well, evaluate economics per successful outcome, keep mappings versioned and replaceable, and never let a model ranking override deterministic policy or authority.**
