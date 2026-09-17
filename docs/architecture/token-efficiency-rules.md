# Codex-Web Token Efficiency Ruleset

## Status

**Architecture policy.** These rules apply to autonomous orchestration, executive reasoning, agent execution, company memory, and any new LLM-driven feature in codex-web.

The guiding principle is:

> **Never use an LLM to determine something codex-web can determine reliably through normal application logic.**

LLMs should be used for judgment, interpretation, planning, synthesis, coding, and reasoning. Deterministic application logic should be used for state management, permissions, routing, filtering, dependency resolution, accounting, and known operational procedures.

## 1. Deterministic-First Rule

Before invoking an LLM, codex-web must determine whether the operation can be completed deterministically.

Use normal application logic for state transitions, dependency checks, permission checks, role lookup, work-item ownership/readiness, budget enforcement, approval requirements, repository/branch/test/CI status, known error classification, event deduplication, scheduling, retry policies, rate limiting, work-graph traversal, resource availability, and known operational procedures.

## 2. Reasoning-Gate Rule

Every potential LLM invocation must pass through a reasoning gate:

```text
Event / Request
      ↓
Can deterministic logic resolve it?
      ├── Yes → Execute normally
      └── No
           ↓
Does it require judgment or reasoning?
      ├── No → Stop / request missing data
      └── Yes → Invoke appropriate model
```

Record why reasoning was required, which role/model was selected, the expected output, the token budget, and the related goal/work item/decision.

## 3. Event-Driven Rule

Autonomous agents must be activated primarily by meaningful events, not repetitive LLM polling.

Good triggers include CI failure, pull request opened/merged, deployment completed/failed, incident created, goal blocked, dependency completed, deadline/budget threshold reached, explicit human analysis requests, customer-feedback thresholds, and security alerts.

An idle system should not periodically ask executive agents whether there is work to do.

## 4. Relevant-Role-Only Rule

Only roles materially relevant to a decision may receive the task. Role selection should be deterministic whenever possible. Do not automatically invoke every executive or specialist.

## 5. No Unbounded Agent Conversations

Agents must not hold open-ended discussions with one another. Prefer parallel independent analysis followed by a single synthesis.

Default limits:

- Maximum participants: `3`
- Maximum reasoning rounds: `1`
- Maximum synthesis rounds: `1`

Higher limits require explicit justification.

## 6. Context-Minimization Rule

Every model invocation must receive the smallest context sufficient to perform the task correctly.

Do not automatically provide entire repository history, full conversation history, all company memory, all architecture decisions, all previous work items, all executive decisions, full CI logs, or unrelated source files.

Prefer:

```text
Current objective
+ Relevant state
+ Relevant files
+ Relevant decisions
+ Relevant errors
+ Relevant constraints
```

## 7. Retrieval-Before-Prompt Rule

Company memory must be retrieval-based rather than injected wholesale into prompts.

Use structured filtering first, then semantic retrieval and ranking. Retrieval should support limits such as:

```yaml
retrieval:
  max_objects: 10
  max_context_tokens: 12000
```

## 8. Structured-State Rule

Important company and execution state must be represented as structured data rather than repeatedly reconstructed from prose.

Structured state should exist for goals, work items, dependencies, roles, authority, decisions, approvals, budgets, projects, services, incidents, and deployments.

## 9. Execution-Checkpoint Rule

Long-running agent sessions must periodically compact their working state into a canonical checkpoint containing the objective, current state, important decisions, blockers, changed files, and next actions.

Future executions should prefer the checkpoint over replaying the entire conversation. Raw history remains available for audit and targeted retrieval.

## 10. Summary-Over-History Rule

Once execution history exceeds a configurable threshold, codex-web must create or update a canonical summary.

Example:

```yaml
context_compaction:
  trigger_tokens: 40000
```

After compaction, prefer canonical summary + recent changes + current task over full historical replay unless older information is specifically required.

## 11. Token Budget Rule

Every LLM-driven operation must have a token budget.

```yaml
reasoning_budget:
  max_input_tokens: 30000
  max_output_tokens: 8000
  max_calls: 4
```

Budgets may be defined at company, project, goal, decision, work-item, and role levels.

## 12. Cost Budget Rule

Token limits should be complemented by financial limits.

```yaml
cost_budget:
  max_cost_usd: 5.00
```

When a work item exceeds its budget, stop or checkpoint, re-evaluate strategy, and continue only when policy permits; otherwise escalate.

## 13. Reasoning-Depth Rule

Reasoning effort must correspond to task impact and complexity.

```text
Level 0 — Deterministic: no LLM
Level 1 — Lightweight: routing, classification, summarization
Level 2 — Standard: implementation and troubleshooting
Level 3 — Deep: architecture and difficult engineering problems
Level 4 — Strategic: high-impact cross-domain decisions
```

The system must not automatically use the highest reasoning level.

## 14. Escalation-Only Rule

More capable or expensive reasoning should normally occur through escalation. Do not begin every task at the highest role/model level.

## 15. Model-Tiering Rule

Use model capability appropriate to the workload:

- Application logic: no model.
- Lightweight model: classification, routing, summarization, context selection.
- Primary coding model: implementation, debugging, testing, review.
- High-reasoning model: architecture, strategic planning, critical decisions, unusual failures.

## 16. Single-Synthesis Rule

Multi-role decisions should normally perform one independent analysis per relevant role followed by one synthesis step. Do not send the synthesis back through every participant unless a material conflict cannot otherwise be resolved.

## 17. Structured-Output Rule

LLMs should return structured outputs whenever the result feeds another system component. Avoid requiring another model call solely to interpret free-form prose.

## 18. Cache-Stable-Reasoning Rule

Reusable conclusions must be persisted, including architecture standards, approved technology choices, service ownership, repository mappings, role mappings, known failure signatures, and operational procedures.

Do not repeatedly ask models to rediscover known facts.

## 19. Known-Pattern Rule

Known incidents and recurring problems should gradually become deterministic procedures. A verified LLM-derived solution should become reusable knowledge or an operational procedure so future occurrences can use deterministic or lower-cost handling.

## 20. Failure-Loop Prevention Rule

Repeated model calls with substantially identical context are prohibited. Detect repeated failures, identical proposed solutions, repeated tool errors, circular delegation, and repeated agent handoffs.

Example defaults:

```yaml
loop_protection:
  max_similar_failures: 3
  max_handoffs: 4
```

After the limit, change strategy, escalate, or require human intervention.

## 21. Context-Diff Rule

When continuing work, prefer the previous checkpoint plus changes since that checkpoint rather than reconstructing the complete project state.

## 22. Source-File Selection Rule

Coding agents should receive only files relevant to the task. Select in this order:

1. Directly referenced files.
2. Files identified through repository search.
3. Direct dependencies.
4. Tests covering those files.
5. Relevant configuration.

Avoid indiscriminate repository-wide context injection.

## 23. Log-Reduction Rule

Raw logs must be filtered before model ingestion:

```text
Raw logs
   ↓
Time/service filter
   ↓
Deduplication
   ↓
Error extraction
   ↓
Relevant surrounding lines
   ↓
LLM
```

Do not routinely send large raw logs directly into prompts.

## 24. Executive-Activation Rule

Executive agents must not be activated for routine engineering work. Executive reasoning is appropriate for company strategy, significant architecture, product direction, major cost, risk, security policy, regulatory concerns, organizational priorities, and cross-project resource allocation.

## 25. Decision-Importance Rule

Decision reasoning budgets must scale with impact.

```yaml
decision_levels:
  low:
    participants: 1
    max_calls: 1
    max_tokens: 10000
  medium:
    participants: 2
    max_calls: 3
    max_tokens: 30000
  high:
    participants: 3
    max_calls: 5
    max_tokens: 75000
  critical:
    participants: 4
    max_calls: 10
    max_tokens: 150000
```

These are defaults and may be overridden by explicit policy.

## 26. Human-Approval-Is-Not-Reasoning Rule

Waiting for human approval must not continuously consume tokens. Persist an `awaiting_approval` state and stop execution until the corresponding approval event arrives.

## 27. Idle-Means-Zero Rule

An idle autonomous system should consume approximately zero LLM tokens. Background deterministic monitoring is allowed; repetitive background LLM polling is not.

## 28. Outcome-Efficiency Rule

Optimize for useful outcomes rather than raw token count. Track tokens per completed work item, successful deployment, resolved incident, accepted decision, and achieved goal.

A high-token operation may still be efficient when the resulting value is high.

## 29. Token Accounting Rule

Every LLM invocation must be attributable to model, agent, role, project, goal, work item, decision, input tokens, output tokens, reasoning calls, estimated cost, and outcome.

No unallocated token consumption should exist.

## 30. Efficiency Dashboard Rule

Codex-web should expose token and cost observability, including model/role/project breakdowns and outcome metrics such as tokens per completed work item or successful PR.

## 31. Efficiency Regression Rule

Changes to autonomous workflows must be evaluated for token regressions. Material increases in tokens per successful outcome require investigation unless they demonstrably improve quality, autonomy, or success rate.

## 32. Context-Quality Rule

Reducing token usage must never remove context required for correctness. The objective is **minimum sufficient context**, not minimum possible context.

When confidence drops because context is insufficient, retrieval should expand incrementally.

## 33. Progressive Retrieval Rule

Start with a small relevant context set and retrieve additional layers only when needed.

```text
Start with top relevant objects
      ↓
Enough information?
 ├── Yes → reason
 └── No  → retrieve next relevant layer
```

Do not retrieve maximum context preemptively.

## 34. Confidence-Based Escalation Rule

Low confidence should trigger additional retrieval or escalation rather than repeated identical reasoning. Thresholds should be risk-sensitive; critical actions require stronger evidence than low-risk analysis.

## 35. Autonomous-Learning Rule

Recurring reasoning should become reusable organizational knowledge:

```text
Unknown problem
      ↓
LLM reasoning
      ↓
Verified solution
      ↓
Knowledge object / procedure
      ↓
Future deterministic or lower-cost handling
```

Token consumption per repeated operation should decrease over time.

## 36. Token-Efficiency Priority Order

When optimizing an expensive workflow, apply improvements in this order:

1. Remove unnecessary model calls.
2. Replace reasoning with deterministic logic.
3. Reduce unnecessary participating roles.
4. Reduce unnecessary reasoning rounds.
5. Improve retrieval relevance.
6. Compact historical context.
7. Reduce raw logs/files provided.
8. Use structured outputs.
9. Use a cheaper suitable model.
10. Reduce output verbosity.

Do not begin by forcing shorter responses if larger architectural inefficiencies remain.

## 37. Required Execution Metadata

Every autonomous LLM execution should record metadata equivalent to:

```yaml
llm_execution:
  goal_id: GOAL-123
  work_item_id: WI-453
  decision_id: null
  role: developer
  model_class: primary_coding
  reasoning_reason: "Novel test failure requiring source analysis"
  context:
    objects_retrieved: 7
    files_included: 4
    checkpoint_used: true
  budget:
    max_input_tokens: 30000
    max_output_tokens: 8000
    max_calls: 4
  usage:
    input_tokens: 12240
    output_tokens: 2810
    calls: 2
  outcome:
    status: success
```

## 38. Default Autonomous Reasoning Policy

Unless explicitly overridden:

```yaml
defaults:
  deterministic_first: true
  context:
    retrieval_first: true
    max_objects: 10
    checkpoint_preferred: true
    include_full_history: false
  agents:
    max_participants: 3
    max_reasoning_rounds: 1
    max_synthesis_rounds: 1
  loops:
    max_retries: 3
    max_handoffs: 4
  executive:
    activate_only_when_material: true
  autonomy:
    idle_llm_calls: false
  accounting:
    token_tracking: required
    cost_tracking: required
```

## 39. Architectural Principle

The autonomous architecture should follow:

```text
                         EVENT
                           │
                           ↓
                 Deterministic Filtering
                           │
                           ↓
                     State / Policy
                           │
             ┌─────────────┴─────────────┐
             │                           │
     Can be resolved normally       Reasoning required
             │                           │
             ↓                           ↓
          Execute                 Retrieve context
                                         │
                                         ↓
                                  Select relevant role
                                         │
                                         ↓
                                   Budgeted reasoning
                                         │
                                         ↓
                                  Structured result
                                         │
                                         ↓
                                Policy / authority check
                                         │
                                         ↓
                                      Execute
                                         │
                                         ↓
                                      Verify
                                         │
                                         ↓
                               Persist useful knowledge
```

## 40. Core Principle

All implementation decisions should ultimately respect this rule:

> **Code manages state. Events trigger work. Retrieval supplies context. Models provide judgment. Policies control authority. Results become reusable knowledge.**

The purpose of token efficiency is not merely to reduce API cost. It is to ensure that codex-web can scale from a coding interface into an autonomous software-company operating system without reasoning costs growing proportionally with company history, agent count, or event volume.

## Development Review Checklist

Any PR that adds or materially changes LLM/agent/autonomy behavior should answer:

- Can any new model call be replaced with deterministic logic?
- What event or user action activates the model call?
- Why are the selected roles required?
- What is the maximum number of model calls/rounds?
- What context is retrieved and what are its limits?
- Is existing state/checkpoint data reused instead of replaying history?
- What token and cost budgets apply?
- How are retries, loops, and repeated failures bounded?
- Is usage attributable to a goal/work item/decision and measurable by outcome?
- Can a verified recurring solution become deterministic or reusable knowledge?

If these questions cannot be answered, the feature should not be considered autonomy-ready.
