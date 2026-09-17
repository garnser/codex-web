# Codex-Web Agent and Developer Instructions

This file is the repository-level entry point for AI agents and developers working on codex-web.

## Required Architecture Policy

Before designing or modifying any LLM-driven, Executive, autonomous, orchestration, memory, routing, integration, definition, or agent-execution behavior, read and follow:

- [`docs/architecture/token-efficiency-rules.md`](docs/architecture/token-efficiency-rules.md)
- [`docs/architecture/README.md`](docs/architecture/README.md) and the architecture/contracts relevant to the change
- [`EXECUTIVE.md`](EXECUTIVE.md) for the current Executive control-plane integration

The token-efficiency rules are **architecture policy**, not optional optimization advice.

## Canonical Delivery Tracking: GitHub

Roadmap status, remaining work, priorities, UI adaptation, and completion are tracked in **GitHub Issues, Milestones, the repository/owner GitHub Project, and Pull Requests**. Do not create or maintain a parallel checkbox roadmap in repository markdown.

The intended tracking model is:

- **Milestone** = delivery phase and dependency ordering.
- **Issue** = independently completable work package with scope and acceptance criteria.
- **GitHub Project** = portfolio/status/priority/risk/area view across milestones.
- **Pull Request** = implementation and validation evidence linked to its issue(s).
- **Architecture docs** = durable contracts, invariants, schemas, boundaries, and design rationale; they are not the source of truth for task completion.

The original roadmap/foundation set is represented by issues `#97`–`#142`, with architecture-review additions `#155`–`#170`. Tracking bootstrap issue `#126` is the canonical issue index for the target Project and Milestones M1–M12. M3 is **Platform, Identity & Safe Execution Foundation** and is a prerequisite for later parallel execution, authority, orchestration, and production autonomy. Cross-milestone UI architecture/delivery is tracked by `#125` and `#127`, with milestone-specific UI work linked from `#127`. Until `#126` is closed, `[M<n>]` issue-title prefixes are the fallback milestone grouping. Once GitHub milestones/project fields exist, their metadata is authoritative and title prefixes are only descriptive.

### Before starting substantial work

1. Inspect open GitHub issues and their milestone/project metadata before inventing new roadmap work.
2. Identify the issue(s) advanced by the proposed change and read their scope, acceptance criteria, dependencies, linked UI work, and linked discussion/PRs.
3. Prefer the earliest ready prerequisite relevant to the request unless the user explicitly directs another issue.
4. If required work is not represented by an issue, create or update an issue before implementation rather than adding a TODO/checklist to an architecture document.
5. If a change spans multiple independently deliverable concerns, split them into separate issues instead of making one issue an unbounded backlog.
6. Do not create parallel identity/session, tenant, secret/key, resource, **definition**, work, permission, decision, policy, configuration, entitlement, model-provider, worker/execution-plane, extension/plugin, UI, provider/action, evidence, or execution systems when an existing canonical primitive can be extended.
7. Before building later-milestone behavior, verify that applicable M3 foundation dependencies already exist or are implemented as part of the same delivery slice.
8. When you encounter a mutable operational/domain catalog hard-coded in Python or JavaScript, evaluate whether it belongs in the canonical Definition Registry tracked by `#170` rather than adding more code-owned definitions.

### Definition vs code boundary

The architectural preference is **definitions are data; schemas, engines, validation, migrations, and hard security invariants remain code**.

Use the database-backed Definition Registry (`#170`) for mutable, reusable definitions that need to be shared across runtime, API, UI, workers, audit, or replay and that should be changeable without a source edit/deploy. Examples include role/execution contracts, expected work, refusal rules, required artifacts, handoff targets, failure conditions, role-selection metadata, workflow/ruleset templates, model-class catalogs, approval templates, and similar operational catalogs.

Keep code ownership for protocol/schema versions, Pydantic/domain schemas, parsers/interpreters, cryptographic verification, migration logic, structural security invariants, fail-closed constraints, and implementation-only constants. Do not store arbitrary executable Python/JavaScript as a definition and do not allow stored definitions to weaken non-negotiable controls.

Keep these concepts distinct:

- **Definition** = reusable versioned domain/runtime description.
- **Configuration** = effective runtime/deployment values and feature rollout state.
- **Policy** = authorization, constraints, approvals, and permitted behavior.
- **Secret/key reference** = protected credential or cryptographic material boundary.
- **Entitlement/quota** = hosted/service capability and consumption limits.
- **Canonical state** = current operational/domain facts.

Executions and other durable reasoning/action records should reference the exact definition IDs/revisions used so historical behavior remains reproducible after definitions change.

### Required UI impact review

Every roadmap issue or PR must explicitly consider whether the change requires a user/operator UI adaptation. UI work is part of product delivery, not optional polish.

1. If a change introduces or materially changes a first-class domain object, identity/session/token, tenant/workspace, mutable definition, secret/key reference, resource, policy, configuration/feature rollout, entitlement/quota, model/provider, worker/execution plane, extension/plugin, authoritative source, provider/action, approval, event/schedule, execution workspace/lease, artifact/evidence, release/upgrade, incident/recovery, audit record, or operator action, determine how users/operators inspect and manage it.
2. Check `#127` and the milestone-specific UI issues before creating duplicate UI work.
3. If required UI work is not already tracked, create or update a linked GitHub issue before the backend/domain issue is closed.
4. User/operator surfaces must expose canonical state, provenance, tenant/resource scope, ownership, effective definition/version where relevant, blockers, authority/denial reasons, triggers, next actions, and verification/results.
5. Canonical codex-web state and synchronized external-provider/worker/extension state must be visibly distinguishable where both exist.
6. High-impact mutations must show relevant target/scope/impact and use canonical identity/step-up, definition, policy, approval, ActionIntent, and provider APIs rather than UI-only checks.
7. New first-class objects should support useful deep links to related Organizations/Workspaces, Definitions, Goals, Decisions, Work Items, roles/agents, resources, events, policies, executions/workers, source records, ActionIntents/provider receipts, artifacts/evidence, releases/upgrades/incidents/recovery, and audit/integrity evidence.
8. Handle applicable loading, empty, error, denied, blocked, invalid/incompatible definition, stale definition/cache, stale/conflict, approval-waiting, unknown-outcome/reconciliation, revoked-session/token/secret/key, quarantined-worker/extension, expired-lease, version-skew/incompatible-upgrade, and success states.
9. Never render stored secret or cryptographic key material back to the UI; display references/metadata and effective use permissions/status only.
10. Preserve keyboard/accessibility behavior and responsive operation for supported workflows.
11. Determine whether screenshots, contextual help, examples, or documentation tracked under M12 must be updated.

A backend/domain slice may merge before its UI slice when that separation is intentional, but required UI follow-up must already be represented and linked in GitHub. Never leave UI adaptation as an implicit future task.

### While implementing

1. Keep the issue scope coherent; record newly discovered follow-up work as GitHub issues rather than silently expanding the current task.
2. Add focused tests for behavioral changes.
3. Update architecture/contracts when implementation changes durable behavior or boundaries.
4. Keep user/operator UI on the same canonical APIs/state/enforcement paths as runtime behavior; never introduce UI-only policy, definition, or execution truth.
5. Mutable operational/domain definitions should be persisted and versioned through the canonical Definition Registry once available; do not add a second hard-coded or file-backed authoritative catalog for the same concept.
6. External side effects must use canonical provider/action boundaries and durable action/reconciliation semantics once those M3 primitives are available; do not add new direct provider mutations that bypass them.
7. Secrets must be passed by reference through a credential boundary, not copied into contracts, prompts, logs, issue bodies, definitions, or ordinary configuration. Cryptographic keys/key material must likewise remain behind the canonical key-management boundary.
8. Untrusted executable work should cross the canonical control-plane/execution-plane worker boundary once available; do not give repository code unrestricted access to control-plane state, credentials, filesystem, or network simply because it is executed by an agent.
9. Extensions/providers must use the shared extension/compatibility/lifecycle contracts once available. Installation, enablement, and authorization are separate operations; extensions may not self-grant authority.
10. For LLM/autonomy work, preserve the token-efficiency, trust-boundary, data-governance, model-routing, definition-versioning, and authority invariants below.
11. Link the implementation PR to the relevant issue(s), including linked UI work when applicable, and state which acceptance criteria it satisfies.

### Validation cadence

Validation is intentionally tiered so development feedback stays fast without weakening the merge gate.

1. **While iterating:** run the smallest focused tests that exercise the behavior being changed. Add or run the relevant Python test module(s), JavaScript syntax checks, and focused browser spec(s) as applicable. Do not rebuild Docker or run the complete browser suite after every intermediate commit unless the change directly requires it or you are debugging a failure.
2. **Before pushing a meaningful PR update:** run the full Python unit suite locally when practical; it is expected to remain a fast deterministic gate. Run relevant browser specs for frontend behavior. Let CI provide the clean-environment Chromium and Docker validation.
3. **Run Docker locally when relevant:** Dockerfile/Compose changes, dependency/runtime packaging, startup/liveness behavior, container path/permission changes, Codex pinning, or investigation of a failing `docker-smoke` CI job. A normal service/model/test edit does not require a local image rebuild after every commit.
4. **CI is the full gate:** PR CI runs independent `unit-static`, `chromium`, and `docker-smoke` jobs. They may execute in parallel and superseded runs may be cancelled when a newer commit exists; only the current head matters.
5. **Before merge and issue closure:** all applicable CI jobs for the current PR head must be green. Never interpret a focused development test as a replacement for the final clean-environment gate.
6. **CI/test-infrastructure changes:** observe a green workflow on the changed pipeline itself before treating the optimization as complete.

The goal is rapid iteration plus one authoritative clean-environment validation at the delivery boundary, not repeated expensive validation after every edit.

### Completion and status rules

1. Do **not** close an issue merely because code exists on a branch.
2. Close an implementation issue only after the required implementation is merged to the default branch, relevant tests/validation are green, and required documentation is current.
3. Before closing a backend/domain issue, verify that any required UI adaptation is either complete or represented by an explicit linked open issue.
4. If a PR only partially satisfies an issue, leave the issue open and update its GitHub discussion/status rather than marking the whole package complete.
5. If implementation changes intended scope or architecture, update the GitHub issue/milestone/project and the affected architecture contract in the same delivery cycle.
6. Completed historical work should remain discoverable through closed issues and merged PRs rather than copied into a markdown completion checklist.
7. When a milestone's completion criteria are satisfied, close/complete the GitHub milestone only after its required backend, UI, test, security, migration, and documentation issues are complete or intentionally deferred with explicit tracking.

## Non-Negotiable Invariants

1. **Deterministic first.** Do not call an LLM for state, permissions, routing, dependency checks, budgets, approvals, known status, or other facts codex-web can calculate reliably.
2. **Idle means zero.** Autonomous LLM activity must be event- or request-driven. An idle system should consume approximately zero model tokens.
3. **Minimum sufficient context.** Retrieve only the context needed for the current task; do not automatically replay complete histories, repositories, logs, or company memory.
4. **Relevant roles only.** Do not invoke Executive or specialist roles unless their domain materially contributes to the decision.
5. **Bound reasoning.** Model calls, participants, rounds, retries, handoffs, token budgets, and cost budgets must have explicit bounds.
6. **Structured state over prose.** Identities, resources, definitions, goals, work items, dependencies, decisions, authority, approvals, budgets, actions, evidence, and execution state belong in structured application state.
7. **Definitions are data; engines are code.** Mutable reusable operational definitions belong in the versioned Definition Registry once available. Schemas, interpreters, migrations, cryptographic logic, protocol versions, and hard security invariants remain code-owned. Stored definitions cannot contain arbitrary executable logic or weaken structural controls.
8. **Exact definition attribution.** Executions, decisions, evaluations, and audits must be able to identify the exact definition IDs/revisions that influenced behavior so later edits do not rewrite historical meaning.
9. **Identity before authority.** Human/service identity, tenant/workspace scope, agent identity, execution-worker identity, and execution role are distinct concepts; authorization must identify the actor and target scope explicitly. Authentication/session assurance must not be confused with authorization.
10. **Secrets by reference.** Agents/providers may be authorized to use credentials without receiving or revealing the raw secret. Secret values must not enter model context or ordinary logs/state unless strictly required by the credential boundary.
11. **Keys stay behind a key boundary.** Encryption/signing key material is not ordinary configuration or a model-visible secret. Persist key references/versions and use canonical KMS/key-management boundaries with explicit rotation/recovery semantics.
12. **Canonical external actions.** Privileged external side effects must flow through provider-neutral action contracts, durable intents/idempotency, authority checks, receipts, and verification rather than ad-hoc API calls.
13. **Control plane is not an execution sandbox.** Untrusted repository/build/tool execution must not implicitly share unrestricted control-plane process/filesystem/network/secrets. Use canonical worker identities, capability/lease/fencing, sandbox, and resource-limit boundaries once available.
14. **Isolated mutable execution.** Concurrent work must not silently share mutable checkouts/resources; use canonical execution workspace/resource ownership and leases where mutation can conflict.
15. **Evidence over assertion.** Completion, approval, and release gates should rely on structured artifacts/evidence and independent verification where possible rather than an agent merely stating that work succeeded.
16. **Trust untrusted content as data.** Task text, repository content, retrieved memory, logs, webhooks, provider responses, tool output, extension output, worker output, and model output cannot grant authority or redefine canonical policy.
17. **Version durable contracts.** Public/admin APIs, canonical schemas/events, persisted contracts/definitions, worker/extension/provider boundaries need explicit compatibility/evolution rules; incompatible versions fail visibly rather than being guessed.
18. **Extensions do not self-authorize.** Installing or enabling an extension does not grant its declared capabilities. Extension provenance, compatibility, configuration, lifecycle, and granted authority remain canonical and auditable.
19. **Govern data lifecycle.** Tenant scope, classification, encryption requirements, retention, redaction/deletion, and derived-data sensitivity must remain enforceable across memory, audit, prompts, logs, definitions where applicable, and artifacts.
20. **Upgrade compatibility is explicit.** Supported version skew, schema/definition migration phases, irreversible boundaries, maintenance/drain behavior, verification, and rollback availability must be declared and tested; do not imply rollback after an incompatible state transition.
21. **Checkpoint long-running work.** Prefer canonical execution summaries/checkpoints plus recent changes over replaying entire agent histories.
22. **Structured outputs.** When model output feeds another component, use a machine-readable contract rather than requiring another model call to interpret prose.
23. **Account for every call.** LLM usage must be attributable to a role, project, goal, work item or decision and measurable against an outcome.
24. **Learn toward determinism.** Repeated verified solutions should become reusable knowledge, signatures, procedures, definitions where appropriate, or deterministic handlers.

## Expected Design Flow

```text
Event / Request
      ↓
Resolve actor + tenant/workspace + target resources
      ↓
Resolve exact published definitions/policy/config needed for this operation
      ↓
Deterministic filtering and state lookup
      ↓
Can normal code resolve it?
 ├── Yes → execute/record deterministic result
 └── No
      ↓
Retrieve minimum authorized context
      ↓
Select only required role/model through canonical definition/model routing
      ↓
Apply token/cost/call budget
      ↓
Reason once where possible
      ↓
Return structured proposal/result
      ↓
Authority + trust-boundary + data-policy check
      ↓
If external mutation: create durable ActionIntent
      ↓
Resolve credential reference + ActionProvider capability
      ↓
If executable work: assign a compatible isolated worker/execution environment
      ↓
Execute in isolated/bounded environment
      ↓
Persist artifacts/provider receipt/evidence + exact definition revisions used
      ↓
Verify outcome
      ↓
Advance canonical state
      ↓
Persist useful knowledge/checkpoint
```

## Pull Request Expectations

Every substantial roadmap PR should identify its GitHub issue(s) and include the UI-impact result: linked UI issue(s), UI included in the PR, or a short explanation of why no UI adaptation is required.

A change that introduces or expands model usage should additionally document, in code comments, tests, PR text, or architecture docs as appropriate:

- Why model reasoning is required.
- Why deterministic logic is insufficient.
- What activates the reasoning path.
- Which role/model class/provider is used and why.
- Maximum calls, rounds, retries, and handoffs.
- Context retrieval limits.
- Token/cost budget behavior.
- Failure and escalation behavior.
- How usage will be attributed and measured.
- Whether successful recurring behavior can later become deterministic.

A change that adds or materially modifies a mutable operational definition should document:

- whether it belongs in the Definition Registry (`#170`) rather than code;
- definition kind/schema/version/scope;
- bootstrap/migration behavior from any existing hard-coded value;
- publish/supersede/rollback behavior and approval requirements;
- compatibility and cache/reload behavior;
- exact-version attribution for execution/audit/replay;
- UI/admin impact;
- why any remaining hard-coded constant must stay code-owned.

A change that adds or expands external side effects should also document:

- actor/tenant/resource scope;
- ActionProvider capability and risk class;
- credential-reference handling;
- idempotency/retry/unknown-outcome behavior;
- rollback/verification requirements;
- produced artifacts/evidence;
- trust-boundary and data-governance implications.

A change that adds or materially modifies executable worker behavior, sensitive persistent data, extensions/plugins, or upgrade/migration behavior should also document the applicable worker isolation/fencing, key/encryption, extension compatibility/authorization, and version-skew/schema/definition-migration/rollback rules rather than inventing a local mechanism.

## Compatibility Principle

The Executive and autonomous layers must continue to use codex-web's canonical execution controls rather than bypassing them. New behavior should build on canonical identities/sessions/workspaces, resources, database-backed definitions, work items, execution contracts, control-plane/worker boundaries, isolated execution, approvals, authority/policy, ActionProviders/ActionIntents, artifacts/evidence, secret/key references, extension lifecycle contracts, explicit upgrade compatibility, and future shared-coordination mechanisms instead of creating parallel execution paths.

## Rule of Thumb

> **Code implements engines and invariants. Definitions describe reusable behavior. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Isolated workers execute bounded work. Actions produce evidence. Results become reusable knowledge.**
