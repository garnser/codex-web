# Codex-Web Architecture

This directory contains architecture decisions, constraints, and cross-cutting policies for codex-web.

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

Repository-level agent instructions are in [`../../AGENTS.md`](../../AGENTS.md).

## Other architecture documents

- [Runtime supervision](runtime-supervision.md)
- [Storage scaling](storage-scaling.md)

## Design review expectation

Any design or PR that adds or materially increases LLM activity should explicitly verify compliance with the Token Efficiency Ruleset. In particular, reviewers should be able to identify:

1. Why reasoning is needed instead of deterministic application logic.
2. What event/request activates the reasoning path.
3. What context is retrieved and how it is bounded.
4. Which roles/models participate and why.
5. Maximum calls, rounds, retries, handoffs, token usage, and cost.
6. How usage is attributed to a goal/work item/decision and measured against an outcome.
7. How repeated successful reasoning can become reusable knowledge or deterministic handling.

The target architecture is:

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**
