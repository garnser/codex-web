# Codex-Web Architecture

This directory contains architecture decisions, constraints, cross-cutting policies, schemas, and durable design contracts for codex-web.

**Delivery status and roadmap completion are not tracked in repository markdown.** GitHub Issues, Milestones, the GitHub Project, and linked Pull Requests are the delivery source of truth. See repository-level instructions in [`../../AGENTS.md`](../../AGENTS.md).

## Required reading for LLM, Executive, and autonomous features

### [Token Efficiency Ruleset](token-efficiency-rules.md)

**Status: Architecture policy.**

Read this before implementing or reviewing any feature involving:

- LLM/model calls
- Codex agent orchestration
- Executive roles or Board reviews
- autonomous execution
- Goal or Decision reasoning
- company/project memory and retrieval
- context construction or compaction
- agent handoffs and escalation
- model routing or model-tier selection
- token/cost budgets

The central rule is:

> **Never use an LLM to determine something codex-web can determine reliably through normal application logic.**

The policy defines deterministic-first execution, event-driven activation, minimum-sufficient context, retrieval-before-prompt, bounded multi-agent reasoning, checkpointing, token/cost accounting, loop protection, progressive retrieval, and outcome-efficiency requirements.

## Delivery tracking

Actionable work is tracked in GitHub Issues, Milestones, the GitHub Project, and linked Pull Requests. This architecture index intentionally does not reproduce milestone names, numbers, phase ordering, or completion state.

Technical dependencies in durable documentation should be expressed in terms of canonical capabilities and contracts—for example identity and tenancy, data governance, authority, eventing, scheduling, safe execution, evidence, model routing, and production qualification. Link an issue or PR when implementation provenance is useful, but do not make a roadmap phase number part of the technical contract.

The platform foundation is intentionally reusable across higher-level capabilities. Identity/tenancy, credential brokering, encryption/key management, canonical resources, provider-neutral external actions, worker trust/isolation, artifacts/evidence, ActionIntent reconciliation, compatibility/versioning, model governance, observability, typed configuration, entitlements/quotas, extensions, versioned definitions, security trust boundaries, and data governance must be consumed rather than reimplemented locally.

### Definitions are data; engines remain code

Mutable operational/domain definitions that need to be shared by runtime, APIs, UI, workers, audit, and replay should be represented as canonical versioned data rather than hard-coded Python catalogs. Issue `#170` owns this Definition Registry foundation and starts with the role/execution-contract catalog currently encoded in `codex_web/execution_contracts.py`.

Examples of data-oriented definitions include role contracts, expected work, refusal rules, required artifacts, handoff targets, failure conditions, role-selection metadata, workflow/ruleset templates, model-class catalogs, and other operational catalogs intended to evolve without a source edit/deploy.

This does **not** mean moving arbitrary program logic into the database. Schemas, Pydantic/domain validators, parsers/interpreters, protocol/schema versions, migration code, cryptographic verification, hard fail-closed constraints, and structural security invariants remain code-owned. Stored definitions must validate against those code-owned contracts and cannot weaken them.

Keep the following concepts distinct even when their administration surfaces are adjacent:

- **Definition** — reusable versioned domain/runtime description, resolved by stable ID/revision.
- **Configuration** — effective runtime/deployment values and feature rollout state.
- **Policy** — authorization, constraints, approvals, and permitted behavior.
- **Secret/key reference** — protected credential or cryptographic material boundary.
- **Entitlement/quota** — hosted/service capability and consumption limits.
- **Canonical state** — current operational/domain facts.

Executions, decisions, evaluations, and audits should retain the exact definition IDs/revisions that influenced them so historical behavior remains reproducible after definitions change.

Event-driven orchestration adds deterministic scheduling (#158), canonical ApprovalRequests/quorum/separation-of-duties (#338), autonomous replay/evaluation (#159), and a canonical human-attention queue (#160). Replay/evaluation must pin the exact Definition Registry revisions used historically. Measured Goal/Decision evidence uses generic provenance-aware Metric/KPI definitions and observations (#339). Production qualification covers releases/supply chain, incidents, backup/disaster recovery, capacity/backpressure, safe upgrades/version skew, audit integrity, and distributed failover where enabled. Governed business operations add business entities/CompanyFacts/external-record references, provider-neutral business-data synchronization, business KPI catalogs, Executive consumption, and Company Operations UI (#340–#345) without turning codex-web into a replacement CRM/billing/accounting system.

Use GitHub tracking as follows:

- **Milestones** define the delivery phases and dependency order.
- **Issues** define independently completable work packages and acceptance criteria.
- **GitHub Project** provides status, priority, risk, area, dependency, and cross-milestone views.
- **Pull Requests** provide implementation and validation evidence linked to issues.
- **Architecture documents** define durable technical truth and should not contain completion checklists that duplicate GitHub state.

If architecture changes materially while implementing an issue, update the relevant architecture contract and GitHub issue/Project state together.

## Architecture documents

- [Canonical execution contract schema](execution-contract-schema.md) — versioned machine-readable contract derived from canonical work-item state.
- [Canonical work-item lifecycle](work-item-lifecycle.md) — existing stages, legal manual/API transitions, external reconciliation boundary, terminal outcomes, and transition failure contract.
- [Dependency-aware Work Graphs](work-graphs.md) — canonical parent/blocking relationships, deterministic readiness, cycle prevention, failure impacts, critical path, traversal, and graph progress.
- [First-class Goals](goals.md) — canonical Goal lifecycle, structured success criteria, budgets, revision history, Work Graph traceability, deterministic progress and health.
- [Canonical Role authority](role-authority.md) — operational Role definitions, atomic scoped grants, inheritance/delegation, deterministic fail-closed evaluation, and exact definition provenance.
- [Canonical event bus](canonical-events.md) — versioned event ingress, durable idempotency, correlation, deterministic filtering, and provider normalization before autonomy.
- [Durable scheduler](durable-scheduler.md) — persistent one-shot/recurring timers, timezone and misfire semantics, fenced leases, crash-safe idempotent firing, and zero-LLM idle behavior.
- [Canonical ApprovalRequest](approval-requests.md) — exact target binding, deterministic quorum/separation-of-duties, scheduler-backed expiry, idempotent decisions and atomic consumption.
- [Canonical human attention](attention.md) — durable deduped operator intervention state, source-domain references, scheduler-backed escalation and provider-neutral delivery.
- [Canonical Agent Providers](agent-providers.md) — provider-neutral model/execution identity, declared-vs-granted capabilities, deterministic discovery and extension/model provenance.
- [Capability-driven agent routing](agent-routing.md) — deterministic, independent model/runtime selection, constraint-preserving fallback, runtime health and exact provider/runtime revision provenance.
- [Provider capacity, quota failover, and resume](provider-capacity.md) — canonical throttled/depleted state, Codex quota probes, bounded model/runtime fallback, scheduler-backed waits, and automatic work resume.
- [Bounded autonomy controller](bounded-autonomy.md) — deterministic-first reasoning gates, loop budgets, kill/dry-run controls, ActionIntent-only side effects, and cycle observability.
- [Tamper-evident autonomy audit and reliability](autonomy-audit.md) — tenant-partitioned hash chains, checkpoints/signing/export, governed redaction, reliability/efficiency metrics, periodic verification and deterministic suspension.
- [Canonical release promotion](releases.md) — immutable build-once promotion, SBOM/provenance/signing gates, exact ApprovalRequests, staged rollout and known-good artifact rollback.
- [Canonical Incident domain](incidents.md) — deduped detection, command/handoff, Attention escalation, ActionIntent containment/recovery, Evidence-required resolution and postmortem learning.
- [Production recovery and continuity](recovery-continuity.md) — explicit RPO/RTO, canonical-key encrypted snapshots, scheduler-backed backups, isolated restore drills, key/audit integrity checks and recovery Evidence.
- [Capacity and resilience qualification](capacity-resilience.md) — shared tenant/global admission leases, critical reserves, workload bulkheads, load shedding, circuits, recovery-storm controls and load-test Evidence.
- [Safe upgrades and version skew](safe-upgrades.md) — explicit release-pair compatibility, preflight/drain, resumable phased migrations, worker/Definition/extension skew checks, irreversible approvals and truthful rollback boundaries.
- [Orchestration inspector](orchestration-inspector.md) — read-only event/cycle timeline and canonical operator controls with zero reasoning on refresh.
- [Autonomous evaluation and deterministic replay](autonomy-evaluation-replay.md) — immutable scenario fixtures, exact definition/runtime/model pins, offline replay, deterministic regression comparison, failure injection, and qualification Evidence.
- [Authoritative task-source contract](task-source-contract.md) — provider-neutral identity, events, capabilities, mapping, reconciliation, project authority, and conformance boundaries for external task systems.
- [GitLab task-source adapter](gitlab-task-source-adapter.md) — GitLab issue identity, discovery/read/event normalization, label-based canonical mapping, and declared write capabilities.
- [Runtime supervision](runtime-supervision.md)
- [Definition Registry](definition-registry.md) — versioned database-backed mutable definitions, lifecycle, compatibility, exact runtime attribution, bootstrap and recovery.
- [Typed configuration and feature rollout](configuration.md) — deterministic scope precedence, versioned publication/rollback, feature targeting, reference values, and configuration-vs-policy/definition boundaries.
- [Input plugin composition pipeline](input-plugin-pipeline.md) — deterministic pluggable request composition, field security classes, bounded provenance, and model-gateway integration.
- [Observability, correlation, and service health](observability.md) — correlation/causation propagation, secret-safe telemetry, bounded metrics, tracing seams, and deterministic readiness/autonomy health.
- [Data governance](data-governance.md) — canonical classification, retention, legal-hold, governed deletion/redaction, export authorization, residency propagation, and model-context filtering boundary.
- [Entitlements, quotas, and usage metering](entitlements.md) — hosted/service capability access, deterministic quota enforcement, idempotent metering, reconciliation, and provider-neutral usage export.
- [Encryption at rest and key management](encryption-key-management.md) — envelope encryption, scoped/versioned key references, rotation/revocation, restore manifests, and pluggable key backends.
- [Execution worker trust boundary](execution-worker-boundary.md) — canonical worker identity/capabilities, bounded assignments, fenced leases, health/drain/quarantine/revocation, and control-plane separation.
- [Extension and plugin lifecycle](extensions.md) — immutable manifests, package verification, compatibility, separate grants/configuration, lifecycle, health quarantine, upgrades and runtime conformance.
- [Model gateway and prompt governance](model-gateway.md) — stable model classes, provider/model registry, deterministic routing/fallback, secret references, prompt revisions, and invocation attribution.
- [Storage scaling](storage-scaling.md)

Additional architecture contracts should be added when their GitHub issues move into implementation; issue state, not this index, remains the delivery source of truth.

## Design review expectation

Any design or PR that adds or materially increases LLM activity should explicitly verify compliance with the Token Efficiency Ruleset and link the GitHub issue(s) whose acceptance criteria it advances. Reviewers should be able to identify:

1. Which issue/work package the change advances.
2. Why reasoning is needed instead of deterministic application logic.
3. What event/request activates the reasoning path.
4. What context is retrieved and how it is bounded.
5. Which roles/models participate and why.
6. Maximum calls, rounds, retries, handoffs, token usage, and cost.
7. How usage is attributed to a Goal, Work Item, or Decision and measured against an outcome.
8. How repeated successful reasoning can become reusable knowledge or deterministic handling.

Any design or PR that adds external side effects should additionally identify the acting identity/tenant, target canonical resource, provider/action capability, credential reference, idempotency/reconciliation behavior, required authority, trust-boundary treatment, execution-worker boundary where applicable, and resulting evidence/verification.

Any design or PR that adds or materially changes mutable operational definitions should identify whether the content belongs in the Definition Registry (#170), the definition schema/version used, migration/bootstrap behavior, exact revision attribution, UI/admin impact, and why any remaining hard-coded value must stay code-owned.

Any design or PR that adds executable worker behavior, durable sensitive data, an extension point, or production upgrade behavior should additionally identify the applicable worker trust/fencing model (#166), encryption/key policy (#167), extension lifecycle/compatibility model (#169), and upgrade/version-skew contract (#168) instead of introducing a local substitute.

The target architecture is:

> **Code implements engines and invariants. Definitions describe reusable behavior. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Isolated workers execute bounded work. Actions produce evidence. Results become reusable knowledge.**

- [Business context and CompanyFacts](business-context.md) — lightweight tenant-scoped business entities, stable external references, selected governed facts, deterministic source authority/conflict handling and canonical links.

- [BusinessDataSource synchronization](business-data-sources.md) — provider-neutral capabilities, normalized events, bounded sync/backfill, cursor safety, tombstones, drift and read-only external authority.

- [Business KPI catalogs and operating views](business-kpis.md) — explicit versioned CompanyFact formulas projected into canonical Metrics, role-oriented starter packs, deterministic current/trend/threshold state and exact Goal/Decision snapshot provenance.

- [Governed Executive business context](executive-business-context.md) — role-specific business domains, bounded governed facts/KPIs, exact recommendation provenance, stale/conflict handling and canonical action boundaries.
