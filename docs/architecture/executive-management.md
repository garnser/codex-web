# Canonical Executive Management

Milestone 9 turns Executive roles from free-form personas into bounded advisory roles over canonical company state. Executive reasoning can recommend or propose changes, but it is never itself authority to mutate Goals, Decisions, Work, provider state, or external systems.

## Role policy

The Executive role catalog is a versioned Definition Registry object (`executive-role-catalog`).

Each role defines:

- stable ID, title, lifecycle, and responsibilities
- canonical object types it may observe
- event subscriptions that can justify activation
- deterministic routing keywords
- bounded consultation peers
- allowed proposal kinds
- whether a proposal is eligible for canonical materialization
- the exact authority capability required for each materialization kind
- role-specific instructions and context/consultation limits

The bootstrap catalog includes Chief of Staff, CTO, CPO, COO, CFO, VP Engineering, Security, CRO, CMO/Growth, and Customer Success. The Definition Registry remains the source of truth once published; later catalog revisions do not rewrite prior activation history.

## Activation and routing

An `ExecutiveActivation` is a durable tenant/workspace-scoped record. It stores:

- trigger kind and trigger reference
- explicit event type when event-driven
- relevant Goal/Decision/Work/Evidence references
- exact Executive role Definition revision
- deterministic role-selection scores and reasons
- a bounded reasoning budget
- exact canonical context snapshot
- specialist consultations and model invocation IDs
- synthesis, disagreement, and escalation state
- advisory proposals
- revision history

Role selection is deterministic before any model call. Explicitly requested roles are honored only within catalog limits. Otherwise subscriptions and keyword matches determine relevant roles. If nothing matches, the catalog's fallback role is selected. Routine work therefore does not invoke every Executive role.

Event subscriptions are routing policy, not polling loops. Codex-web does not run free-running Executive model checks. A request, canonical event, or scheduled review must explicitly create an activation.

## Canonical context

Executive reasoning receives only explicitly referenced canonical state that the selected role policy permits:

- Goal snapshots
- Decisions
- Work Items
- WorkGraph snapshots
- Evidence

References are resolved inside the authenticated tenant/workspace before model invocation. Missing or cross-tenant references fail closed. Context is wrapped as untrusted data before it is passed to the Model Gateway; task text, evidence text, provider output, and model output cannot grant authority.

This M9 layer intentionally does not make the legacy Executive chat transcript a source of company state.

## Bounded multi-role consultation

A consultation performs:

1. one strategic Model Gateway call for each selected role, in parallel
2. at most one synthesis call when more than one role participates
3. exact JSON/schema validation of every specialist output
4. durable recording of role outputs and model invocation IDs
5. explicit preservation of material disagreement and escalation

The activation budget caps model calls, input tokens, output tokens, and cost. Creation fails before any model invocation when the budget cannot cover the selected roles plus required synthesis.

A role output may contain advisory proposals only. It cannot claim that a Goal, Decision, Work Item, approval, or external action has already happened.

## Materialization and authority

Executive proposals remain `proposed` until a separate canonical materialization request succeeds.

Supported materialization paths are deliberately narrow:

- Goal proposal -> canonical `GoalService.create`
- Decision proposal -> canonical `DecisionService.create`
- Work proposal -> canonical `DecisionWorkService.commit`

Before materialization, codex-web evaluates the authenticated actor through the canonical operational Role/authority service using the capability required by the Executive role catalog. An authority denial leaves the proposal advisory and performs no company-state mutation.

Authority grants that require qualifying approvals remain denied until those approvals are satisfied by canonical policy. Executive Management does not implement a second quorum system.

Work proposals additionally require a Decision ID and pass through the M8 Decision-to-Work bridge. That bridge requires the Decision to be canonically approved and creates external task-source work only through durable ActionIntent/ActionProvider execution. Executive code never calls an external provider directly.

Goal and Decision records created from Executive proposals store the trusted activation/proposal origin on the canonical record. Before creating either object, the materialization bridge searches for that exact origin and reuses it. This makes retries safe even if canonical creation succeeded but the Executive proposal bookkeeping update was interrupted.

Escalation proposals are never automatically materialized into company state. They remain human/action-routing signals for the canonical Attention/Approval/Decision workflows.

## Compatibility

The older Executive chat/delegation API remains available during migration, but its `/api/executive/agents` metadata is sourced from the canonical Executive role catalog when the catalog service is installed. Existing execution-role delegation remains governed by the separate canonical execution-role contract and Work Item ownership state.

The new canonical management API lives under `/api/executive/roles` and `/api/executive/activations...`. Milestone 9 UI work in issue #116 should visualize these records rather than creating a dashboard-owned Executive lifecycle.
