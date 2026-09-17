# Codex-Web Agent and Developer Instructions

This file is the repository-level entry point for AI agents and developers working on codex-web.

## Required Architecture Policy

Before designing or modifying any LLM-driven, Executive, autonomous, orchestration, memory, routing, or agent-execution behavior, read and follow:

- [`docs/architecture/token-efficiency-rules.md`](docs/architecture/token-efficiency-rules.md)
- [`EXECUTIVE.md`](EXECUTIVE.md) for the current Executive control-plane integration

The token-efficiency rules are **architecture policy**, not optional optimization advice.

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
