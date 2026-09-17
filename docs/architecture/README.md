# Codex-Web Architecture

This directory contains architecture decisions, constraints, cross-cutting policies, and the canonical development roadmap for codex-web.

## Required reading for LLM, Executive, and autonomous features

### [Token Efficiency Ruleset](token-efficiency-rules.md)

**Status: Architecture policy.**

Read this before implementing or reviewing any feature involving:

- LLM/model calls
- Codex agent orchestration
- Executive roles or Board reviews
- Autonomous execution
- Goal or decision reasoning
- Company/project memory and retrieval
- Context construction or compaction
- Agent handoffs and escalation
- Model routing or model-tier selection
- Token/cost budgets

The central rule is:

> **Never use an LLM to determine something codex-web can determine reliably through normal application logic.**

The policy defines deterministic-first execution, event-driven activation, minimum-sufficient context, retrieval-before-prompt, bounded multi-agent reasoning, checkpointing, token/cost accounting, loop protection, progressive retrieval, and outcome-efficiency requirements.

### [Autonomous Company Roadmap](autonomy-roadmap.md)

**Status: Canonical development roadmap.**

The roadmap defines the intended progression and checkable subtasks for:

1. Executive-contract foundation.
2. Work-item lifecycle and execution contracts.
3. Dependency-aware work graphs.
4. First-class goals.
5. Role authority and permission contracts.
6. Event-driven autonomous orchestration.
7. First-class decision objects.
8. Executive management through goals, decisions, and work graphs.
9. Durable organizational/company memory.
10. Controlled production autonomy, budgets, approvals, auditability, and progressive rollout.

Developers and agents should identify the milestone/subtask their work advances and respect the dependency order. Roadmap checkboxes should only be marked complete after the implementation is merged, required tests are green, and documentation is current.

Repository-level agent instructions are in [`../../AGENTS.md`](../../AGENTS.md).

## Other architecture documents

- [Canonical execution contract schema](execution-contract-schema.md) — versioned Milestone 2 machine-readable contract derived from canonical work-item state.
- [Canonical work-item lifecycle](work-item-lifecycle.md) — existing stages, legal manual/API transitions, GitLab reconciliation boundary, and transition failure contract.
- [Runtime supervision](runtime-supervision.md)
- [Storage scaling](storage-scaling.md)

## Design review expectation

Any design or PR that adds or materially increases LLM activity should explicitly verify compliance with the Token Efficiency Ruleset and identify the relevant roadmap milestone. In particular, reviewers should be able to identify:

1. Which roadmap milestone/subtask the change advances.
2. Why reasoning is needed instead of deterministic application logic.
3. What event/request activates the reasoning path.
4. What context is retrieved and how it is bounded.
5. Which roles/models participate and why.
6. Maximum calls, rounds, retries, handoffs, token usage, and cost.
7. How usage is attributed to a goal/work item/decision and measured against an outcome.
8. How repeated successful reasoning can become reusable knowledge or deterministic handling.

The target architecture is:

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**
