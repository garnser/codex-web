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

Actionable autonomous-company work has been migrated to GitHub Issues. Issues `#97`–`#125` represent the initial migration of the former roadmap and cross-milestone UI scope. Tracking bootstrap issue `#126` defines the target GitHub Project and Milestones M1–M11.

Use GitHub tracking as follows:

- **Milestones** define the delivery phases and dependency order.
- **Issues** define independently completable work packages and acceptance criteria.
- **GitHub Project** provides status, priority, risk, area, and cross-milestone views.
- **Pull Requests** provide implementation and validation evidence linked to issues.
- **Architecture documents** define durable technical truth and should not contain completion checklists that duplicate GitHub state.

If architecture changes materially while implementing an issue, update the relevant architecture contract and GitHub issue/Project state together.

## Architecture documents

- [Canonical execution contract schema](execution-contract-schema.md) — versioned machine-readable contract derived from canonical work-item state.
- [Canonical work-item lifecycle](work-item-lifecycle.md) — existing stages, legal manual/API transitions, external reconciliation boundary, terminal outcomes, and transition failure contract.
- [Authoritative task-source contract](task-source-contract.md) — provider-neutral identity, events, capabilities, and adapter boundary for GitLab and future authoritative task systems.
- [Runtime supervision](runtime-supervision.md)
- [Storage scaling](storage-scaling.md)

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

The target architecture is:

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**
