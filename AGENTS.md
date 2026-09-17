# Codex-Web Agent and Developer Instructions

This file is the repository-level entry point for AI agents and developers working on codex-web.

## Required Architecture Policy

Before designing or modifying any LLM-driven, Executive, autonomous, orchestration, memory, routing, or agent-execution behavior, read and follow:

- [`docs/architecture/token-efficiency-rules.md`](docs/architecture/token-efficiency-rules.md)
- [`docs/architecture/README.md`](docs/architecture/README.md) and the architecture/contracts relevant to the change
- [`EXECUTIVE.md`](EXECUTIVE.md) for the current Executive control-plane integration

The token-efficiency rules are **architecture policy**, not optional optimization advice.

## Canonical Delivery Tracking: GitHub

Roadmap status, remaining work, priorities, and completion are tracked in **GitHub Issues, Milestones, the repository/owner GitHub Project, and Pull Requests**. Do not create or maintain a parallel checkbox roadmap in repository markdown.

The intended tracking model is:

- **Milestone** = delivery phase and dependency ordering.
- **Issue** = independently completable work package with scope and acceptance criteria.
- **GitHub Project** = portfolio/status/priority/risk/area view across milestones.
- **Pull Request** = implementation and validation evidence linked to its issue(s).
- **Architecture docs** = durable contracts, invariants, schemas, boundaries, and design rationale; they are not the source of truth for task completion.

The migrated roadmap work is currently represented by issues `#97`–`#125`. Tracking bootstrap issue `#126` defines the target Project and Milestones M1–M11. Until `#126` is closed, `[M<n>]` issue-title prefixes are the fallback milestone grouping. Once GitHub milestones/project fields exist, their metadata is authoritative and title prefixes are only descriptive.

### Before starting substantial work

1. Inspect open GitHub issues and their milestone/project metadata before inventing new roadmap work.
2. Identify the issue(s) advanced by the proposed change and read their scope, acceptance criteria, dependencies, and linked discussion/PRs.
3. Prefer the earliest ready prerequisite relevant to the request unless the user explicitly directs another issue.
4. If required work is not represented by an issue, create or update an issue before implementation rather than adding a TODO/checklist to an architecture document.
5. If a change spans multiple independently deliverable concerns, split them into separate issues instead of making one issue an unbounded backlog.
6. Do not create parallel state, work, permission, decision, policy, UI, or execution systems when an existing canonical primitive can be extended.

### While implementing

1. Keep the issue scope coherent; record newly discovered follow-up work as GitHub issues rather than silently expanding the current task.
2. Add focused tests for behavioral changes.
3. Update architecture/contracts when implementation changes durable behavior or boundaries.
4. Keep user/operator UI on the same canonical APIs/state/enforcement paths as runtime behavior; never introduce UI-only policy or execution truth.
5. For LLM/autonomy work, preserve the token-efficiency and authority invariants below.
6. Link the implementation PR to the relevant issue(s), and state which acceptance criteria it satisfies.

### Completion and status rules

1. Do **not** close an issue merely because code exists on a branch.
2. Close an implementation issue only after the required implementation is merged to the default branch, relevant tests/validation are green, and required documentation is current.
3. If a PR only partially satisfies an issue, leave the issue open and update its GitHub discussion/status rather than marking the whole package complete.
4. If implementation changes intended scope or architecture, update the GitHub issue/milestone/project and the affected architecture contract in the same delivery cycle.
5. Completed historical work should remain discoverable through closed issues and merged PRs rather than copied into a markdown completion checklist.
6. When a milestone's completion criteria are satisfied, close/complete the GitHub milestone only after its required issues are complete.

## Non-Negotiable Invariants

1. **Deterministic first.** Do not call an LLM for state, permissions, routing, dependency checks, budgets, approvals, known status, or other facts codex-web can calculate reliably.
2. **Idle means zero.** Autonomous LLM activity must be event- or request-driven. An idle system should consume approximately zero model tokens.
3. **Minimum sufficient context.** Retrieve only the context needed for the current task; do not automatically replay complete histories, repositories, logs, or company memory.
4. **Relevant roles only.** Do not invoke Executive or specialist roles unless their domain materially contributes to the decision.
5. **Bound reasoning.** Model calls, participants, rounds, retries, handoffs, token budgets, and cost budgets must have explicit bounds.
6. **Structured state over prose.** Goals, work items, dependencies, decisions, authority, approvals, budgets, and execution state belong in structured application state.
7. **Checkpoint long-running work.** Prefer canonical execution summaries/checkpoints plus recent changes over replaying entire agent histories.
8. **Structured outputs.** When model output feeds another component, use a machine-readable contract rather than requiring another model call to interpret prose.
9. **Account for every call.** LLM usage must be attributable to a role, project, goal, work item or decision and measurable against an outcome.
10. **Learn toward determinism.** Repeated verified solutions should become reusable knowledge, signatures, procedures, or deterministic handlers.

## Expected Design Flow

```text
Event / Request
      ↓
Deterministic filtering and state lookup
      ↓
Can normal code resolve it?
 ├── Yes → execute/record result
 └── No
      ↓
Retrieve minimum relevant context
      ↓
Select only required role/model
      ↓
Apply token/cost/call budget
      ↓
Reason once where possible
      ↓
Return structured result
      ↓
Authority/policy check
      ↓
Execute and verify
      ↓
Persist useful knowledge/checkpoint
```

## Pull Request Expectations

Every substantial roadmap PR should identify its GitHub issue(s). A change that introduces or expands model usage should additionally document, in code comments, tests, PR text, or architecture docs as appropriate:

- Why model reasoning is required.
- Why deterministic logic is insufficient.
- What activates the reasoning path.
- Which role/model is used and why.
- Maximum calls, rounds, retries, and handoffs.
- Context retrieval limits.
- Token/cost budget behavior.
- Failure and escalation behavior.
- How usage will be attributed and measured.
- Whether successful recurring behavior can later become deterministic.

## Compatibility Principle

The Executive layer must continue to use codex-web's existing execution controls rather than bypassing them. New autonomous behavior should build on canonical work items, execution contracts, approvals, sandbox controls, and authority/policy mechanisms instead of creating parallel execution paths.

## Rule of Thumb

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**
