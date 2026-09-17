# Cross-Milestone UI Adaptation Requirements

## Status

**Architecture guidance for roadmap Milestones 2–11.** This document defines the UI adaptations required as codex-web evolves from a thread-centric Codex client into a bounded autonomous software-company operating system.

These requirements are not a separate deferred UI phase. Each UI capability belongs to the roadmap milestone whose canonical backend/domain capability it exposes. The UI must remain a projection and control surface over canonical application state; it must never introduce a parallel state, policy, work, authority, or execution model.

## Why the information architecture must evolve

The current interface is primarily organized around Projects, Threads, a Developer panel, and an Executive drawer. That is appropriate for the current product, but later roadmap milestones make Work Items, Work Graphs, Goals, Roles, Policies, Events, Decisions, Memory, Approvals, and Autonomy first-class objects.

As those objects become real, continuously adding cards, nested details, or drawers would make the interface difficult to understand and operate. The UI should therefore evolve toward first-class workspaces with consistent navigation and deep links while preserving the fast conversational/thread workflow for ordinary Codex use.

Recommended top-level product areas are:

- **Home / Overview** — system health, attention-required items, recent activity, and current autonomous work.
- **Projects** — repositories, task-source bindings, project state, and project-scoped configuration.
- **Work** — work items, queues, handoffs, execution state, contracts, and dependency graphs.
- **Goals** — desired outcomes, decomposition, progress, constraints, budgets, and linked work.
- **Decisions** — structured decisions, evidence, approvals, dissent, consequences, and resulting work.
- **Organization** — roles, agents, Executive functions, authority, rulesets, and effective policy.
- **Memory** — company/project knowledge, provenance, lifecycle, relationships, and retrieval inspection.
- **Autonomy** — triggers, events, approvals, budgets, simulations, audit trails, and pause controls.
- **Threads** — conversational Codex execution and direct human collaboration.
- **Settings / Integrations** — provider connections, authoritative task sources, runtime configuration, and administrative settings.

The exact navigation may evolve, but the product should not force every future first-class object into the existing thread view or Developer panel.

## Cross-cutting UI invariants

1. **Canonical UI state.** UI views and actions use the same canonical APIs/models used by runtime enforcement. No shadow UI state or duplicated policy model.
2. **Operator-visible causality.** For autonomous or delegated work, users can determine what triggered it, which goal/decision/work item it belongs to, which role acted, which effective policy allowed it, and what happened as a result.
3. **Provider provenance.** Canonical state and external-provider state are visually distinguishable. The UI never implies that provider state is canonical when it is merely synchronized input.
4. **Explainable permissions.** Hidden or disabled actions should expose why they are unavailable when doing so is useful and safe; denials should identify the relevant policy/authority boundary.
5. **Progressive disclosure.** Common workflows remain simple. Advanced policy, reconciliation, audit, and autonomy controls are available without dominating routine use.
6. **Consistent vocabulary.** Goal, work-item, decision, role, event, approval, and execution states use shared status components and canonical terminology.
7. **Object deep links.** Goals, decisions, work items, agents/roles, events, policies, executions, source records, and artifacts can link to one another without requiring manual search.
8. **Safe mutation.** High-impact actions show scope/impact before execution and use the canonical approval model. Reversible actions expose rollback/recovery where supported.
9. **Event/data-driven refresh.** UI freshness must not require periodic LLM calls. Streaming, events, API polling, or user refresh may update deterministic application state.
10. **Documentation-ready UI.** Important workflows should support stable demo/sample data and repeatable screenshots for Milestone 11 documentation.
11. **Accessibility and responsive operation.** New first-class workspaces must remain keyboard-usable, screen-reader understandable, and workable on narrower screens where practical.

## Milestone 2 — Work Item Lifecycle and Authoritative Task Sources

The work-item UI should become an operator cockpit rather than only a selector in Executive delegation.

- [ ] Add a canonical work-item detail view showing stage, owner, handoff, blockers, execution state, retry/deadline information, and failure classification.
- [ ] Show authoritative source type/instance, external ID/link, provider revision/event cursor, synchronization status, and last successful synchronization.
- [ ] Visually separate canonical codex-web state from external task-source state.
- [ ] Add reconciliation/conflict diagnostics for stale events, provider write failures, split-brain findings, and unsupported provider capabilities.
- [ ] Add permission-gated retry/resync/reconcile actions that flow through canonical reconciliation services.
- [ ] Add a chronological work-item event/history timeline with actor/source/reason and before/after state where available.
- [ ] Show the effective execution contract, role/binding, sandbox, approval policy, success criteria, and failure conditions used for an execution.
- [ ] Surface execution checkpoints/resume state for long-running work.
- [ ] Surface attributable token/cost usage when accounting hooks exist.

## Milestone 3 — Dependency-Aware Work Graphs

- [ ] Add an interactive dependency graph with parent/child and `blocks` / `blocked_by` relationships.
- [ ] Add graph editing using canonical dependency APIs with cycle/conflict validation before save.
- [ ] Show deterministic `why blocked?` and `why runnable?` explanations.
- [ ] Highlight critical-path work and work that can safely execute in parallel.
- [ ] Show graph/project progress and downstream impact of failures or replanning.
- [ ] Support filters, zoom/focus, and deep links from graph nodes to work-item detail.

## Milestone 4 — First-Class Goals

- [ ] Add a Goal workspace for creating and editing outcomes, success criteria, constraints, deadlines, budgets, risk limits, and approval requirements.
- [ ] Show goal health/progress and deterministic reasons for `at_risk` or `blocked` states.
- [ ] Add a decomposition preview before generated work is committed to the canonical work graph.
- [ ] Allow authorized users to accept, revise, or reject proposed decomposition/work.
- [ ] Show end-to-end `goal -> decision/project -> work -> result` traceability.
- [ ] Make goal changes/version history and the reason for scope/success-criteria changes inspectable.

## Milestone 5 — Roles, Authority, Rulesets, and Policies

The roadmap already requires a role/contract/policy management UI. In addition:

- [ ] Add an effective-policy explorer across global, workspace/project, role, agent, and work-item scopes.
- [ ] Add a policy simulator answering deterministic questions such as `Would role/agent X be allowed to perform action Y on resource Z?` with the exact allow/deny reasoning.
- [ ] Add side-by-side version diffs for rulesets/contracts/policies.
- [ ] Add role/agent/project authority matrices for quickly spotting unexpected grants or missing access.
- [ ] Add dry-run/impact simulation for policy changes before publication.
- [ ] Clearly distinguish inherited, overridden, explicitly denied, and locally granted rules.

## Milestone 6 — Event-Driven Autonomous Orchestration

- [ ] Add an event stream/inspector showing normalized event type, source, correlation ID, deduplication/idempotency status, and resulting action.
- [ ] Add trigger/rule configuration using canonical deterministic trigger definitions.
- [ ] Show the autonomous-cycle trace: `observe -> filter -> reason (if required) -> authority check -> execute -> verify`.
- [ ] Explicitly show why a model was invoked or why deterministic handling avoided a model call.
- [ ] Add retry/backoff, dead-letter, and failed-event views with safe replay where supported.
- [ ] Allow correlation-ID drilldown across events, work, executions, decisions, and external provider records.
- [ ] Add pause/resume, dry-run, and simulation controls with clear scope and current state.

## Milestone 7 — First-Class Decisions

- [ ] Add a Decision workspace showing the question, evidence, assumptions, constraints, options, risks, recommendation, final decision, and confidence/uncertainty metadata.
- [ ] Show participating roles, approvals, dissenting views, deliberation rounds, and reasoning budget consumption.
- [ ] Add decision lifecycle/history and supersession views.
- [ ] Show decision consequences and deep links to generated goals/projects/work.
- [ ] Add post-execution review comparing expected and actual outcomes.

## Milestone 8 — Executive Management

- [ ] Add an Executive/role roster showing responsibility, current authority, subscriptions, active work, and availability/state.
- [ ] Add a `why activated?` explanation for Executive participation in an event, goal, or decision.
- [ ] Add cross-role handoff and escalation queues.
- [ ] Show Executive-generated proposals, goals, decisions, and resulting work in one activity timeline.
- [ ] Expose workload/activity without turning the UI into an invitation for unnecessary always-on model activity.

## Milestone 9 — Company Memory

The current knowledge editor can remain useful for simple manual entries, but first-class organizational memory needs broader inspection and lifecycle controls.

- [ ] Add browse/search with scope, type, source, tags, project, freshness, and authority filters.
- [ ] Display provenance/citations and links back to source material.
- [ ] Clearly mark stale, superseded, invalidated, or conflicting knowledge.
- [ ] Add safe supersede/deprecate workflows instead of destructive replacement where historical provenance matters.
- [ ] Add relationship navigation/knowledge graph where it materially improves understanding.
- [ ] Add a retrieval inspector showing the exact knowledge/context selected for a reasoning run, subject to authorization.

## Milestone 10 — Controlled Autonomy

- [ ] Build a unified Autonomy Control Center rather than scattering high-impact controls across Developer and Executive panels.
- [ ] Show effective autonomy level by global, project, role, agent, and action scope.
- [ ] Add a causality/audit explorer: `event -> goal/decision -> role -> policy -> contract -> action -> verification/result`.
- [ ] Add approval inboxes with impact context and links to the action/policy being approved.
- [ ] Add budget drilldowns for model/tokens, money, actions, and other configured limits.
- [ ] Add reliability/promotion-readiness views showing the metrics required before autonomy can increase.
- [ ] Add impact simulation before autonomy-level or production-scope increases.
- [ ] Add global/project/agent pause state prominently, including why a scope is paused and who/what paused it.
- [ ] Add recovery/rollback surfaces for supported actions and repeated-failure suspension diagnostics.

## Milestone 11 — Documentation and Adoption

- [ ] Add a first-run onboarding checklist/wizard that leads to a verifiable first successful task without hiding important security/authority choices.
- [ ] Add contextual help, glossary links, and concise `what happens next` explanations for unfamiliar states.
- [ ] Add guided demo/sample project data covering work items, a goal, a decision, a policy, and an autonomous cycle.
- [ ] Design useful empty states that explain what the object is, why it matters, and the next action.
- [ ] Deep-link documentation to exact product areas and product help back to the relevant documentation.
- [ ] Maintain stable sanitized demo fixtures so screenshots and walkthroughs can be regenerated consistently.

## Navigation and frontend evolution

The existing frontend can evolve incrementally, but new first-class domains should not all be injected into one increasingly large `index.html`, `app.js`, Developer panel, or Executive drawer.

As the roadmap advances:

- Introduce a reusable application shell/navigation model before several new first-class workspaces arrive.
- Split domain UI modules around canonical product objects rather than around ad-hoc panels.
- Reuse shared components for status, provenance, policy explanations, timelines, approvals, object links, and destructive/high-impact actions.
- Keep conversational Threads available as a primary execution surface, but do not make a thread the required container for Goals, Decisions, Policies, Memory, or autonomous operations.
- Preserve direct URLs/deep links for first-class objects so documentation, audit records, notifications, and external systems can point to exact state.

## Definition of UI-complete for roadmap features

A roadmap feature with material user/operator interaction should not be considered fully complete until, where applicable:

1. Its canonical state is inspectable in the UI.
2. Authorized actions are available without requiring raw API calls or code edits.
3. State provenance, denial reasons, and failure conditions are understandable.
4. Related first-class objects are navigable by deep link.
5. Loading, empty, error, conflict, permission-denied, and stale-data states are handled.
6. High-impact changes show impact and approval requirements before execution.
7. UI behavior has focused automated coverage appropriate to the feature.
8. User/operator documentation and screenshots are updated under Milestone 11 expectations.
