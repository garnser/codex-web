# Codex-Web Agent and Developer Instructions

This file is the repository-level entry point for AI agents and developers working on codex-web.

## Required Architecture Policy and Roadmap

Before designing or modifying any LLM-driven, Executive, autonomous, orchestration, memory, routing, or agent-execution behavior, read and follow:

- [`docs/architecture/token-efficiency-rules.md`](docs/architecture/token-efficiency-rules.md)
- [`docs/architecture/autonomy-roadmap.md`](docs/architecture/autonomy-roadmap.md)
- [`docs/architecture/ui-adaptation-requirements.md`](docs/architecture/ui-adaptation-requirements.md) for roadmap-related user/operator UI and first-class product workspaces
- [`EXECUTIVE.md`](EXECUTIVE.md) for the current Executive control-plane integration

The token-efficiency rules are **architecture policy**, not optional optimization advice. The autonomy roadmap is the **canonical development sequence** for evolving codex-web toward bounded autonomous company operation. Roadmap-related UI must expose the same canonical state and enforcement paths defined by the backend architecture rather than introducing UI-only state, permissions, policy, or execution semantics.

## Roadmap-Driven Development

For substantial improvement work, identify the milestone and subtask being advanced before implementation.

1. Inspect the current code and tests before assuming a roadmap item is incomplete or complete.
2. Prefer the earliest incomplete prerequisite relevant to the requested change.
3. Do not create parallel state, work, permission, decision, or execution systems when an existing canonical primitive can be extended.
4. Later-milestone work may proceed only when its required earlier primitives already exist or are implemented as part of the work.
5. Keep changes small and coherent where possible: complete one meaningful subtask rather than partially implementing several future layers.
6. Add focused tests for behavioral roadmap work.
7. Update architecture/contracts when implementation changes their meaning.
8. Mark a roadmap checkbox complete only after the implementation is merged into the default branch, required tests are green, and documentation is current.
9. If implementation changes the intended architecture, update the roadmap in the same PR rather than allowing code and roadmap to drift.
10. Every roadmap milestone remains subject to the token-efficiency and authority invariants below.

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

A change that introduces or expands model usage should document, in code comments, tests, PR text, or architecture docs as appropriate:

- Which roadmap milestone/subtask it advances.
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

The Executive layer must continue to use codex-web's existing execution controls rather than bypassing them. New autonomous behavior should build on canonical work items, execution contracts, approvals, sandbox controls, and future authority/policy mechanisms instead of creating parallel execution paths.

## Rule of Thumb

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**
