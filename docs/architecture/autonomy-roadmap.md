# Codex-Web Autonomous Company Roadmap

## Status

**Canonical development roadmap.** This document defines the intended progression from codex-web's current execution and Executive capabilities toward a bounded autonomous software-company operating system.

It is intended for both developers and agents. Before starting a substantial architecture, Executive, orchestration, work-management, memory, or autonomy change, identify which milestone and subtask the work advances.

This roadmap must be implemented together with the [Token Efficiency Ruleset](token-efficiency-rules.md) and the repository-level [Agent and Developer Instructions](../../AGENTS.md).

## How to use this roadmap

- Work generally proceeds in milestone order because later milestones depend on primitives established by earlier ones.
- Parallel work is acceptable when dependencies are clear and no later milestone introduces a competing execution path.
- A checkbox may be marked complete only when the implementation is merged into the default branch, relevant tests are green, and required documentation is updated.
- A partially implemented feature remains unchecked. Use an issue/PR reference next to the item when useful.
- Do not mark an entire milestone complete until its completion criteria are satisfied.
- If implementation reveals that a milestone needs to change, update this document in the same PR that changes the architectural direction.
- Token efficiency, authority boundaries, auditability, deterministic-first behavior, and user/operator documentation are cross-cutting requirements for every milestone.

## Dependency order

```text
1. Executive Contract
        ↓
2. Work Item Lifecycle + Task Sources
        ↓
3. Work Graph
        ↓
4. Goals
        ↓
5. Authority Model
        ↓
6. Autonomous Orchestration
        ↓
7. Decisions
        ↓
8. Executive Management
        ↓
9. Company Memory
        ↓
10. Controlled Autonomy
        ↓
11. Product Documentation + Adoption
```

---

## Milestone 1 — Finish and Merge the Executive-Contract Foundation

**Objective:** make Executive-to-Codex delegation deterministic, role-safe, reusable, and fully tested before building broader autonomy on top of it.

**Status:** ✅ Complete. The canonical Executive-contract integration was merged in PR #47 and the required Python, frontend/Chromium, Docker, and smoke validation was green before merge. Subsequent schema/lifecycle work on `main` further hardens the same canonical path.

### Subtasks

- [x] Complete canonical-role derivation.
- [x] Reject conflicting or incompatible role assignments.
- [x] Reuse existing agent/thread bindings when appropriate.
- [x] Finalize pending-handoff routing.
- [x] Complete GitLab-driven execution-contract injection.
- [x] Add focused tests for canonical-role derivation.
- [x] Add focused tests for conflicting-role rejection.
- [x] Add focused tests for existing-binding reuse.
- [x] Add focused tests for pending-handoff routing.
- [x] Add focused tests for GitLab-driven contract injection.
- [x] Add negative-path tests for invalid role/contract combinations.
- [x] Run the full Python test suite.
- [x] Run the full JavaScript/Chromium test suite.
- [x] Run Docker/integration tests.
- [x] Fix regressions discovered by the full suite.
- [x] Document the Executive execution contract and lifecycle.
- [x] Verify backward compatibility with existing thread/queue/sandbox/approval behavior.
- [x] Merge the Executive-contract changes only after the required suites are green.

### Completion criteria

- Executive delegation has one canonical role/contract path.
- Invalid or conflicting role assignments fail closed.
- Existing bindings are reused deterministically.
- Handoffs and GitLab-derived contracts are covered by tests.
- No parallel Executive execution path bypasses normal codex-web controls.

---

## Milestone 2 — Formalize the Work Item State Machine and Execution Contract

**Objective:** make every unit of work have a deterministic lifecycle, contract, owner, audit trail, completion path, and provider-neutral authoritative task source.

**Status:** 🚧 In progress. The versioned canonical execution-contract schema and the canonical manual/API work-item transition policy are merged on `main`; state-mutation centralization, provider-neutral task-source abstraction, richer terminal states, audit/event history, checkpoints, cost hooks, migration, and UI work remain open.

### Subtasks

- [x] Define canonical work-item states such as `created`, `ready`, `assigned`, `running`, `blocked`, `review`, and `completed`.
- [ ] Define terminal states including `completed`, `cancelled`, and `failed`.
- [x] Define allowed state transitions.
- [x] Reject illegal state transitions.
- [ ] Create a single authoritative state-transition service.
- [ ] Remove direct state mutations scattered through the codebase.
- [x] Define the canonical execution-contract schema.
- [x] Include role in the execution contract.
- [x] Include agent identity/binding in the execution contract.
- [x] Include repository and branch scope.
- [x] Include environment/sandbox scope.
- [x] Include permissions and approval requirements.
- [x] Include inputs and expected outputs.
- [x] Include explicit success criteria.
- [x] Version the execution-contract schema.
- [x] Validate execution contracts before work starts.
- [ ] Add retry metadata and retry policy fields.
- [ ] Add timeout/deadline support.
- [ ] Add failure-reason classification.
- [ ] Add work-item event history.
- [ ] Add audit metadata showing who or what changed state.
- [ ] Add execution summaries/checkpoints for long-running work.
- [ ] Add token/cost accounting hooks to the execution contract.
- [x] Add tests for every valid and invalid state transition.
- [ ] Add migration support for existing work items.
- [ ] Add UI indicators for state, owner, blockers, and execution status.

### Authoritative task-source abstraction

GitLab is the current authoritative external task source, but canonical codex-web work-item behavior must not depend on GitLab being the provider. GitLab should become one adapter implementing a provider-neutral task-source contract so other authoritative systems can be selected without creating a second execution model.

- [ ] Define a provider-neutral `WorkItemSource` / `TaskSource` contract for authoritative task systems.
- [ ] Separate canonical work-item identity from external source identity and persist source type, source instance, external ID/URL, and source revision/event cursor as provenance.
- [ ] Move GitLab issue discovery, labels, assignees, status, comments, and reconciliation behind a GitLab task-source adapter.
- [ ] Define explicit adapter capabilities for discovery/read, event/webhook ingestion, owner/state write-back, comments, and artifact links; unsupported capabilities must be declared rather than assumed.
- [ ] Define deterministic mappings between provider-specific state/owners/labels and canonical codex-web work-item fields.
- [ ] Make stale-event handling, idempotency, conflict detection, and split-brain handling provider-neutral at the reconciliation boundary.
- [ ] Allow the authoritative task source to be configured per project/workspace without changing core work-item, Executive, queue, or execution logic.
- [ ] Ensure exactly one authoritative external source owns mutable task state for a work item unless an explicit future federation policy is configured.
- [ ] Keep provider-specific API objects and terminology out of the canonical execution contract and core state-transition interfaces.
- [ ] Add shared adapter contract/conformance tests that every authoritative task-source implementation must pass.
- [ ] Add at least one non-GitLab adapter or complete provider-neutral reference adapter to prove GitLab is not a special case.
- [ ] Preserve backward compatibility and migration for existing GitLab-backed work-item references and persisted state.
- [ ] Add source provenance, authority, synchronization status, and conflict diagnostics to relevant APIs/UI.

### Completion criteria

- Work state can only change through one authoritative mechanism.
- Every executable work item has a validated, versioned contract.
- State history and execution provenance are inspectable.
- Long-running work can resume from a checkpoint instead of replaying full history.
- Core work-item, routing, contract, and execution logic does not depend on GitLab-specific schemas, labels, or API semantics.
- GitLab operates through the same provider-neutral authoritative task-source contract available to alternative providers.
- Source provenance and conflict rules make it deterministic which external system is authoritative for each work item.
- At least one additional adapter or provider-neutral reference implementation passes the shared task-source conformance suite.

---

## Milestone 3 — Implement Dependency-Aware Work Graphs

**Objective:** allow codex-web to understand sequencing, blocking, parallelism, and project-level progress without asking an LLM to reconstruct those facts.

### Subtasks

- [ ] Add parent/child relationships between work items.
- [ ] Add explicit dependency relationships.
- [ ] Support `blocks` and `blocked_by` semantics.
- [ ] Add dependency-cycle detection.
- [ ] Prevent execution when required dependencies are incomplete.
- [ ] Automatically mark work ready when dependencies clear.
- [ ] Add project-level work graphs.
- [ ] Add graph traversal utilities.
- [ ] Add a deterministic `what can run now?` query.
- [ ] Add a deterministic `why is this blocked?` query.
- [ ] Add critical-path calculation.
- [ ] Support explicitly parallelizable work items.
- [ ] Add dependency failure-propagation rules.
- [ ] Define downstream behavior for dependency failure: pause, fail, re-plan, or escalate.
- [ ] Add graph-level progress calculation.
- [ ] Add APIs for creating and modifying dependencies.
- [ ] Add graph visualization in the UI.
- [ ] Add tests for fan-in graphs.
- [ ] Add tests for fan-out graphs.
- [ ] Add tests for parallel execution eligibility.
- [ ] Add tests for dependency cycles.
- [ ] Add tests for partial failures.
- [ ] Add tests for deep work graphs.

### Completion criteria

- Readiness and blocking are calculated deterministically from graph state.
- Work can safely execute in parallel where dependencies permit.
- Users and agents can explain why work is runnable or blocked without model reasoning.

---

## Milestone 4 — Introduce First-Class Goals Above Work Items

**Objective:** allow humans and authorized agents to express desired outcomes while codex-web maintains traceable work underneath them.

### Subtasks

- [ ] Create a `Goal` domain model.
- [ ] Add title and description.
- [ ] Add owner.
- [ ] Add status and priority.
- [ ] Add target date/deadline support.
- [ ] Add measurable success criteria.
- [ ] Add constraints.
- [ ] Add token/compute/cost budgets.
- [ ] Add acceptable-risk metadata.
- [ ] Add human-approval requirements.
- [ ] Link goals to projects.
- [ ] Link goals to work graphs.
- [ ] Allow one goal to produce multiple projects/work graphs.
- [ ] Add goal decomposition logic.
- [ ] Add `goal -> proposed work` generation.
- [ ] Apply a reasoning budget and maximum planning depth to decomposition.
- [ ] Require generated work to retain traceability to the originating goal.
- [ ] Add deterministic progress calculation from subordinate work where possible.
- [ ] Add goal-health states such as `on_track`, `at_risk`, and `blocked`.
- [ ] Add goal evaluation and completion verification.
- [ ] Allow goals to be revised.
- [ ] Record why goal scope or success criteria changed.
- [ ] Add goal dashboards.
- [ ] Add tests for goal decomposition.
- [ ] Add tests for goal completion verification.
- [ ] Add tests for goal revision and traceability.

### Completion criteria

- Every autonomous work chain can trace back to an explicit goal or authorized request.
- Goal success criteria are machine-readable where possible.
- Goal decomposition is bounded by token/call/planning-depth budgets.

---

## Milestone 5 — Implement Role Authority and Permission Contracts

**Objective:** make autonomy bounded by explicit, testable, UI-manageable authority and agentic contracts rather than implicit trust in prompts or hard-coded role behavior.

### Subtasks

- [ ] Define a canonical `Role` model.
- [ ] Separate role identity from agent identity.
- [ ] Define capability and permission primitives.
- [ ] Add permission levels such as `read`, `recommend`, `prepare`, `execute`, and `approve`.
- [ ] Define resource scopes.
- [ ] Define repository scopes.
- [ ] Define environment scopes.
- [ ] Distinguish production from staging/test environments.
- [ ] Add monetary limits.
- [ ] Add token/model-use limits.
- [ ] Add action-risk classifications.
- [ ] Add maximum autonomous-risk levels.
- [ ] Add approval requirements.
- [ ] Add role inheritance where appropriate.
- [ ] Add project-specific permission overrides.
- [ ] Add temporary delegated authority.
- [ ] Add authority expiration.
- [ ] Validate authority before every external or state-changing action.
- [ ] Add explicit denial reasons.
- [ ] Record policy decisions in the audit trail.
- [ ] Add tests proving agents cannot exceed repository scope.
- [ ] Add tests proving agents cannot exceed environment scope.
- [ ] Add tests proving agents cannot exceed financial/token limits.
- [ ] Add tests proving approval requirements cannot be bypassed.

### Contract, policy, goal, and role management UI

The canonical role/authority system should be operable through codex-web rather than requiring source-code edits for normal policy changes. The UI must edit the same structured configuration consumed by runtime enforcement; it must not create a second UI-only policy model.

- [ ] Treat role definitions, agentic contract rulesets, constraints, permissions, scopes, budgets, approval requirements, and role bindings as versioned structured configuration.
- [ ] Build an administration UI to create, edit, clone, disable/deprecate, and inspect roles and agentic contracts.
- [ ] Allow role rulesets/instructions, expected work, prohibited actions, required artifacts, failure conditions, and handoff policies to be managed through the UI.
- [ ] Allow permissions, repository/resource/environment scopes, autonomy/risk limits, approval policies, token/model limits, and monetary limits to be managed through the UI.
- [ ] Allow canonical Goals and their constraints, success criteria, budgets, approvals, and role/project bindings to be created and managed through the UI without duplicating the Milestone 4 Goal model.
- [ ] Support configuration inheritance and overrides across global, workspace/project, role, and agent scopes with a deterministic precedence model.
- [ ] Show the effective resolved contract/policy for a selected role, agent, project, or work item, including where each inherited rule originated.
- [ ] Add validation that rejects contradictory permissions, invalid scopes, impossible handoff rules, missing required fields, or authority combinations that would fail closed at runtime.
- [ ] Add a pre-publish impact view showing which agents, projects, active work items, and autonomous behaviors would be affected by a policy change.
- [ ] Support draft, validate, publish, supersede, and rollback lifecycle for contract/policy versions.
- [ ] Require configured approvals for sensitive authority expansions, production scopes, budget increases, or autonomy-level increases before publication.
- [ ] Record immutable/auditable before-and-after diffs, actor, timestamp, approval evidence, and change reason for every published policy revision.
- [ ] Allow safe import/export of versioned contract/ruleset configuration for review, backup, and environment promotion.
- [ ] Add tests proving UI/API edits compile to the same canonical runtime contract and enforcement behavior.
- [ ] Add tests for inheritance/override precedence, invalid policy rejection, approval-gated publication, rollback, and effective-policy rendering.

### Completion criteria

- No agent action relies solely on prompt instructions for authorization.
- Every privileged action can explain which authority contract allowed or denied it.
- Sensitive actions fail closed when authority is missing or ambiguous.
- Normal role, ruleset, permission, goal, constraint, scope, and budget changes can be managed through the UI without code changes or deployment.
- UI/API configuration and runtime enforcement use one canonical versioned policy/contract model with no shadow configuration path.
- Administrators can inspect the effective policy for any agent/work item and trace each rule to its source scope/version.
- Policy changes are validated, impact-visible, auditable, approval-gated where required, and reversible.

---

## Milestone 6 — Create Event-Driven Autonomous Orchestration

**Objective:** allow codex-web to observe meaningful events and initiate bounded work while keeping idle LLM usage approximately zero.

### Subtasks

- [ ] Create an internal event bus or canonical event-dispatch abstraction.
- [ ] Define canonical event types.
- [ ] Emit events for work-item transitions.
- [ ] Emit events for pull requests.
- [ ] Emit events for CI/pipeline results.
- [ ] Emit events for deployments.
- [ ] Emit events for incidents and failures.
- [ ] Add webhook ingestion.
- [ ] Add polling adapters only where events are unavailable.
- [ ] Build the autonomy controller.
- [ ] Separate observation from reasoning.
- [ ] Separate reasoning from execution.
- [ ] Add deterministic event filtering before model invocation.
- [ ] Add a reasoning threshold/gate.
- [ ] Add trigger rules.
- [ ] Add event deduplication.
- [ ] Add idempotency keys.
- [ ] Add event correlation IDs.
- [ ] Add retry/backoff behavior.
- [ ] Add dead-letter handling.
- [ ] Add cooldowns for repeated event classes.
- [ ] Add maximum recursion/task-depth limits.
- [ ] Add maximum actions per cycle.
- [ ] Add pause/resume controls.
- [ ] Add a global kill switch.
- [ ] Add dry-run mode.
- [ ] Add simulation mode.
- [ ] Add observability for every autonomous cycle.
- [ ] Add tests for repeated/duplicate events.
- [ ] Add tests proving deterministic events do not invoke an LLM unnecessarily.
- [ ] Add tests proving one event cannot create an infinite work loop.

### Completion criteria

- Idle codex-web produces no periodic Executive/agent reasoning traffic.
- Events pass through deterministic filters and authority checks before reasoning/execution.
- Repeated events are idempotent and bounded.

---

## Milestone 7 — Introduce First-Class Decision Objects

**Objective:** make important reasoning durable, inspectable, reviewable, and capable of producing authorized work without relying on ephemeral agent conversations.

### Subtasks

- [ ] Create a `Decision` domain model.
- [ ] Store the question being decided.
- [ ] Store the initiating user, role, or system component.
- [ ] Add decision participants.
- [ ] Add evidence references.
- [ ] Add assumptions.
- [ ] Add constraints.
- [ ] Support multiple options.
- [ ] Store pros/cons per option.
- [ ] Store risk analysis.
- [ ] Store recommendation separately from final decision.
- [ ] Track confidence/uncertainty.
- [ ] Add approval requirements.
- [ ] Add dissenting views.
- [ ] Add decision states such as `draft`, `analysis`, `awaiting_approval`, `approved`, `rejected`, and `superseded`.
- [ ] Add expiration/review dates.
- [ ] Add consequences/actions.
- [ ] Allow approved decisions to generate projects/work.
- [ ] Maintain `goal -> decision -> work -> result` traceability.
- [ ] Add decision history/versioning.
- [ ] Add decision review after execution.
- [ ] Add participant limits.
- [ ] Add deliberation-round limits.
- [ ] Default multi-role reasoning to parallel analysis plus one synthesis.
- [ ] Scale token/call budgets with decision importance.
- [ ] Add tests for approval flows.
- [ ] Add tests for rejected decisions.
- [ ] Add tests for superseded decisions.
- [ ] Add tests for bounded multi-role reasoning.

### Completion criteria

- Important decisions are first-class records rather than chat-only artifacts.
- Decision reasoning has explicit participants, budgets, evidence, authority, and outcome traceability.
- Approved decisions can create work only through canonical work-item/work-graph paths.

---

## Milestone 8 — Connect Executive Roles to Goals, Decisions, and Work Graphs

**Objective:** turn Executive roles into bounded management functions that operate through canonical company objects rather than independent role-playing agents.

### Subtasks

- [ ] Define concrete Executive roles such as CTO, CPO, COO, CFO, and other needed specialists.
- [ ] Define each role's authority contract.
- [ ] Define which information each Executive can observe.
- [ ] Define which event types each Executive subscribes to.
- [ ] Define Executive responsibilities.
- [ ] Add Executive-specific system contracts/instructions.
- [ ] Add structured Executive outputs.
- [ ] Prevent free-form role-play from directly mutating company state.
- [ ] Require Executive actions to flow through Goals, Decisions, Work Items, and authority checks.
- [ ] Add deterministic relevant-role selection where possible.
- [ ] Add multi-role consultation.
- [ ] Add structured disagreement handling.
- [ ] Add decision escalation rules.
- [ ] Add quorum/approval rules where needed.
- [ ] Allow Executives to create proposed goals.
- [ ] Allow authorized Executives to create work.
- [ ] Allow authorized Executives to re-plan blocked projects.
- [ ] Allow Executives to monitor outcomes.
- [ ] Add periodic Executive reviews only when event/schedule policy justifies them; do not add free-running LLM polling.
- [ ] Add role dashboards.
- [ ] Add tests for cross-role handoffs.
- [ ] Add tests for relevant-role-only activation.
- [ ] Add tests ensuring Executives cannot bypass authority checks.
- [ ] Add tests ensuring Executive delegation reuses canonical execution controls.

### Completion criteria

- Executive roles manage canonical company objects instead of separate chat state.
- Routine engineering work does not automatically activate Executive reasoning.
- Cross-role work is bounded, auditable, and authority-controlled.

---

## Milestone 9 — Build Durable Organizational and Company Memory

**Objective:** give agents and Executives relevant institutional knowledge without injecting the entire company history into every prompt.

### Subtasks

- [ ] Define knowledge-object types.
- [ ] Add architecture decisions.
- [ ] Add policies.
- [ ] Add products.
- [ ] Add repositories.
- [ ] Add services.
- [ ] Add incidents.
- [ ] Add postmortems.
- [ ] Add customers/account context where appropriate and authorized.
- [ ] Add projects.
- [ ] Add goals.
- [ ] Add decisions.
- [ ] Add canonical identifiers.
- [ ] Add source provenance.
- [ ] Add timestamps and authorship.
- [ ] Add versioning.
- [ ] Add semantic indexing/search.
- [ ] Add structured metadata filtering/search.
- [ ] Add relationships between knowledge objects.
- [ ] Add project-scoped memory.
- [ ] Add company-scoped memory.
- [ ] Add role/authority-aware retrieval.
- [ ] Add retention policies.
- [ ] Add handling for superseded or invalid knowledge.
- [ ] Prevent stale knowledge from silently overriding current state.
- [ ] Add memory ingestion from Git repositories.
- [ ] Add memory ingestion from PRs/issues.
- [ ] Add architecture-decision ingestion.
- [ ] Add retrieval before Executive reasoning.
- [ ] Add retrieval before major technical changes.
- [ ] Add retrieval budgets and Top-K/context-token limits.
- [ ] Add progressive retrieval when initial context is insufficient.
- [ ] Add citations/provenance to agent reasoning outputs.
- [ ] Add mechanisms to convert verified recurring solutions into reusable procedures/known-pattern handlers.
- [ ] Add tests showing agents respect previous decisions and policies.
- [ ] Add tests for stale/superseded knowledge handling.
- [ ] Add tests for retrieval boundaries and authorization.

### Completion criteria

- Memory is retrieval-based, provenance-aware, and bounded.
- Agents can find relevant prior decisions without replaying full history.
- Repeated solved problems can progressively move toward deterministic handling.

---

## Milestone 10 — Add Autonomy Controls, Auditability, Budgets, Approvals, and Progressive Rollout

**Objective:** safely increase autonomous execution while retaining human control, explainability, cost control, and rollback capability.

### Subtasks

- [ ] Define autonomy levels globally.
- [ ] Define autonomy levels per role.
- [ ] Define autonomy levels per project.
- [ ] Define autonomy levels per action type.
- [ ] Support staged modes such as `observe`, `recommend`, `prepare`, `execute_low_risk`, and broader execution levels.
- [ ] Add approval gates.
- [ ] Add monetary budgets.
- [ ] Add token/compute budgets.
- [ ] Add cloud-spend limits.
- [ ] Add maximum autonomous-action limits.
- [ ] Add production-change limits.
- [ ] Add maintenance-window policies.
- [ ] Add two-person approval for configured critical actions.
- [ ] Add rollback requirements.
- [ ] Add pre-flight checks.
- [ ] Add post-action verification.
- [ ] Add immutable or append-only audit records suitable for reconstruction of autonomous actions.
- [ ] Log the reason for every autonomous action.
- [ ] Log which goal/decision authorized each action.
- [ ] Log the exact authority/policy contract used.
- [ ] Log model/token/cost usage for every reasoning path.
- [ ] Add an `Explain this action` UI.
- [ ] Add an autonomy dashboard.
- [ ] Add a pending-approval dashboard.
- [ ] Add a budget-consumption dashboard.
- [ ] Add token/cost efficiency dashboards.
- [ ] Add incident/escalation dashboard.
- [ ] Add global pause.
- [ ] Add per-agent pause.
- [ ] Add per-project pause.
- [ ] Add automatic suspension after repeated failures.
- [ ] Define reliability thresholds required before increasing autonomy.
- [ ] Track autonomous task-completion rate.
- [ ] Track human-intervention rate.
- [ ] Track rollback rate.
- [ ] Track tokens/cost per successful outcome.
- [ ] Add efficiency-regression checks for autonomy changes.
- [ ] Exercise staged rollout in simulation/dry-run before broader execution.

### Completion criteria

- Autonomy is configurable rather than binary.
- Every autonomous action is attributable, explainable, bounded, and recoverable.
- Autonomy can be paused globally or locally.
- Higher autonomy levels are enabled only after measured reliability thresholds are met.

---

## Milestone 11 — Build End-to-End Product Documentation and Adoption Guidance

**Objective:** make codex-web understandable and adoptable from the first installation through advanced autonomous operation, with task-oriented documentation, realistic worked examples, expected outcomes, screenshots, and operator guidance that stays synchronized with the product.

Documentation is a product surface, not an afterthought. This milestone consolidates the full learning journey, while every earlier milestone must document new user-visible concepts as they are introduced.

### Documentation foundation

- [ ] Define a documentation information architecture for `Getting Started`, `Core Concepts`, `Tutorials`, `How-to Guides`, `Administration`, `Operations`, `Reference`, `Troubleshooting`, and `Advanced Adoption`.
- [ ] Create a documentation landing page that routes readers by persona and maturity level: evaluator, individual developer, team lead, administrator, platform/operator, and autonomous-operations owner.
- [ ] Define a consistent documentation template covering objective, prerequisites, steps, expected result, verification, common failure modes, rollback/recovery, and related concepts.
- [ ] Establish versioned documentation so users can identify which codex-web release a guide or screenshot applies to.
- [ ] Add documentation ownership/review rules so user-visible changes cannot be considered complete while their affected documentation is stale.
- [ ] Add link/reference validation and documentation quality checks to CI where practical.

### Getting started and first success

- [ ] Document prerequisites, supported deployment modes, installation, configuration, startup, health verification, upgrading, and clean rollback/uninstall paths.
- [ ] Create a guided first-run path from a fresh install to a working codex-web instance.
- [ ] Document how to connect/configure the first repository/project/workspace.
- [ ] Document how to configure the first authoritative task source, beginning with GitLab and expanding as task-source adapters are added.
- [ ] Create a `first successful task` tutorial showing task discovery, ownership, execution, validation/handoff, completion, and where the resulting state is visible.
- [ ] Explain exactly what the user should expect at every step, including which state changes, messages, artifacts, approvals, and UI indicators should appear.
- [ ] Provide a minimal safe starter configuration that demonstrates the product without granting unnecessary authority.

### Core product mental model

- [ ] Explain the relationship between projects/workspaces, authoritative task sources, canonical work items, queues, threads, agents, execution roles, and execution contracts.
- [ ] Explain lifecycle stages, ownership, handoffs, validation, blockers, reconciliation, and split-brain behavior with concrete examples.
- [ ] Explain Goals, work graphs/dependencies, Decisions, Executive roles, company memory, authority contracts, approvals, budgets, and autonomy levels as those features become available.
- [ ] Explain the difference between advisory Executive reasoning and operational execution authority.
- [ ] Explain canonical state versus external-provider state and how synchronization/reconciliation is expected to behave.
- [ ] Include diagrams showing the normal flow from `goal/request -> authoritative task -> canonical work item -> execution -> validation -> result`.

### Progressive adoption paths

- [ ] Create an `Adoption Level 1` guide for supervised/manual execution with minimal automation.
- [ ] Create an `Adoption Level 2` guide for structured roles, contracts, handoffs, and validation.
- [ ] Create an `Adoption Level 3` guide for goals, work graphs, task-source integration, and cross-agent coordination.
- [ ] Create an `Adoption Level 4` guide for Executive functions, decisions, company memory, and policy-managed delegation.
- [ ] Create an `Adoption Level 5` guide for bounded autonomous operation with budgets, approvals, monitoring, rollback, and incident response.
- [ ] For each adoption level, document prerequisites, recommended configuration, new capabilities, risks, expected behavior, observable success signals, and criteria for moving to the next level.
- [ ] Include guidance for adopting codex-web incrementally in an existing engineering organization without requiring an all-at-once workflow migration.

### Worked examples and recipes

- [ ] Add a complete small-feature implementation example from incoming task through merged/closed result.
- [ ] Add a bug-fix example including reproduction, implementation, validation failure, handback, correction, and completion.
- [ ] Add a blocked-work example showing exact blocker ownership and recovery.
- [ ] Add a multi-agent handoff example showing implementation, validation, CI/runtime intervention, and release lanes.
- [ ] Add a Goal-to-work decomposition example showing generated work, dependencies, progress, and completion criteria.
- [ ] Add an Executive/Decision example showing advisory analysis, approval, canonical work creation, and execution traceability.
- [ ] Add a policy-management example showing how to change a role's permissions/constraints safely through the UI, preview impact, publish, verify effective policy, and roll back.
- [ ] Add an authoritative-task-source example showing GitLab configuration and, once available, how the same workflow maps to a second provider.
- [ ] Add autonomy examples showing observe/recommend/prepare/execute-style modes and what changes between them.
- [ ] Include examples of both successful runs and intentionally failing scenarios so operators know what healthy refusal, blocking, escalation, and approval behavior look like.

### Screenshot and visual documentation

- [ ] Capture annotated screenshots for installation/onboarding UI, project setup, task-source setup, work-item state, queues/threads, Executive UI, role/policy management, goals, decisions, approvals, autonomy controls, audit/history, and troubleshooting views as those surfaces become available.
- [ ] Keep screenshots close to the instructions they illustrate rather than collecting them in a separate gallery.
- [ ] Add callouts/arrows/captions only where they materially clarify what the user should click or inspect.
- [ ] Ensure screenshots use sanitized demo data with no secrets, real credentials, customer data, or private repository content.
- [ ] Define a repeatable screenshot-refresh workflow so UI changes can update documentation imagery efficiently.
- [ ] Prefer reproducible demo fixtures/sample projects so screenshots and examples can be regenerated consistently.
- [ ] Treat materially stale screenshots as documentation defects.

### Administration and product management

- [ ] Document how to create, inspect, change, version, publish, supersede, and roll back roles, contracts, rulesets, permissions, constraints, scopes, budgets, and approvals.
- [ ] Document inheritance and override precedence with examples showing the effective resolved policy for global, project, role, agent, and work-item contexts.
- [ ] Document how to create and manage Goals, success criteria, budgets, role/project bindings, and goal revisions.
- [ ] Document task-source configuration, provider capabilities, synchronization status, authority conflicts, and migration between supported providers where applicable.
- [ ] Document how to manage autonomy levels, pauses, kill switches, approval queues, budgets, maintenance windows, and reliability thresholds.
- [ ] Document audit and explainability views, including how to answer `why did this agent act?`, `which policy allowed it?`, `what changed?`, and `what did it cost?`.
- [ ] Provide recommended baseline configurations for individual development, small teams, larger engineering organizations, and production autonomous operation.

### Operations, troubleshooting, and recovery

- [ ] Create an operator runbook for startup, shutdown, upgrades, backups, restore, migrations, log collection, health checks, and dependency verification.
- [ ] Document common failure modes for task sources, agent bindings, queues, model providers, sandboxes, approval flows, CI/runtime integrations, and state reconciliation.
- [ ] Provide symptom-to-diagnosis troubleshooting guides with exact commands/UI checks and expected healthy output where appropriate.
- [ ] Document how to recover from stale state, failed handoffs, split brain, duplicate events, stuck work, failed validation, unavailable task providers, and model/provider outages.
- [ ] Document safe pause, rollback, and incident-containment procedures for autonomous operation.
- [ ] Document backup/restore and disaster-recovery expectations for canonical state and policy/configuration data.

### Reference and deep technical adoption

- [ ] Maintain configuration/environment-variable reference documentation with defaults, valid values, security implications, and examples.
- [ ] Maintain API documentation for supported public/administrative APIs with example requests and responses.
- [ ] Document canonical schemas for work items, execution contracts, roles/authority, goals, decisions, task-source adapters, events, and audit records.
- [ ] Document extension points and adapter contracts for task sources, model providers, external events, and future integrations.
- [ ] Provide architecture/developer documentation explaining major services, state boundaries, data ownership, execution flow, and invariants.
- [ ] Cross-link user-facing concepts to deeper architecture documentation without requiring normal users to understand implementation details.

### Documentation verification and product integration

- [ ] Add a maintained demo/sample environment or fixture set used by tutorials and screenshots.
- [ ] Test critical getting-started/tutorial flows against supported releases so documented steps produce the documented outcomes.
- [ ] Add documentation checks to release readiness for changed user-visible workflows, configuration, policy semantics, or APIs.
- [ ] Add contextual `Help`/documentation links from complex UI surfaces where practical.
- [ ] Collect documentation gaps discovered during support, onboarding, incidents, and product adoption as trackable work.
- [ ] Define a process for deprecating or redirecting obsolete documentation when product behavior changes.

### Completion criteria

- A new user can go from zero installation knowledge to a verified first successful codex-web task by following the documentation without undocumented setup steps.
- A team can progress from supervised operation to advanced bounded autonomy through explicit adoption guides with prerequisites and expected outcomes.
- Every major UI workflow has task-oriented documentation and current screenshots where visual guidance materially improves understanding.
- Worked examples cover normal success, handoffs, validation failures, blocking, approvals, policy changes, and autonomous-operation scenarios.
- Administrators can operate roles, goals, task sources, permissions, constraints, budgets, approvals, and autonomy controls using documented procedures rather than source-code knowledge.
- Operators have documented troubleshooting, recovery, backup/restore, upgrade, and incident-containment procedures.
- Documentation is version-aware, release-reviewed, testable where practical, and treated as part of product quality rather than optional prose.

---

# Epic grouping

## Epic 1 — Reliable Execution Foundation

Milestones 1–3.

**Goal:** make codex-web a deterministic and reliable execution engine with canonical contracts, provider-neutral task authority, state, and dependency-aware work.

## Epic 2 — Autonomy Foundation

Milestones 4–5.

**Goal:** give codex-web first-class goals plus explicit authority and policy boundaries that are safely manageable through canonical APIs/UI.

## Epic 3 — Autonomous Organization

Milestones 6–8.

**Goal:** allow codex-web to observe events, reason when needed, make structured decisions, and delegate canonical work through bounded Executive roles.

## Epic 4 — Organizational Intelligence

Milestone 9.

**Goal:** provide durable, retrieval-based institutional knowledge with provenance and bounded context.

## Epic 5 — Production Autonomy

Milestone 10.

**Goal:** progressively enable autonomous execution with budgets, approvals, auditability, explainability, rollback, and measurable reliability.

## Epic 6 — Product Adoption and Operability

Milestone 11.

**Goal:** make codex-web approachable from first installation through deep production adoption with progressive learning paths, worked examples, current screenshots, administration guidance, and reliable operator documentation.

# Cross-cutting requirements

Every milestone must preserve the following invariants:

1. **Deterministic first:** if application logic can answer the question reliably, do not invoke a model.
2. **Idle means zero:** no repetitive background LLM polling.
3. **Minimum sufficient context:** retrieve only what is needed for the current task.
4. **Relevant roles only:** do not invoke agents or Executives without a material reason.
5. **Bound reasoning:** cap calls, rounds, retries, handoffs, context, tokens, and cost.
6. **Canonical state:** goals, work, decisions, authority, approvals, and budgets live in structured application state.
7. **Canonical execution path:** Executive/autonomous features must use existing/canonical work, queue, sandbox, approval, and execution mechanisms rather than bypassing them.
8. **Provider-neutral task authority:** GitLab, GitHub, Jira, Linear, or any future authoritative task system must integrate through a provider adapter; provider-specific concepts must not leak into canonical work-item or execution semantics.
9. **Single policy truth:** UI, API, agents, and runtime enforcement must consume the same versioned contract/policy objects; UI configuration must never become a shadow policy path.
10. **Documentation is part of done:** user-visible behavior, configuration, policy semantics, APIs, and operator workflows must be documented alongside implementation, with screenshots/examples updated when they materially affect understanding.