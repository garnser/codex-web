# Adoption Levels 1–5

> Applies to: current main  
> Audience: engineering leaders, administrators and operators

Codex-web is designed for incremental adoption. Do not enable broad autonomy merely because the underlying models or providers can execute actions.

## Level 1 — supervised interactive use

**Prerequisites**

- healthy local/single-instance installation;
- project workspace configured;
- human identity present;
- sandbox and approval settings understood.

**Capabilities**

- interactive Codex threads;
- human-reviewed commands/file changes;
- local repository tasks;
- read-only Executive analysis where configured.

**Primary risks**

- accepting an unsafe command;
- selecting the wrong repository;
- treating model output as verified truth.

**Observable success**

- users can complete bounded tasks;
- repository-native validation passes;
- unexpected actions are rejected;
- no external credentials are required for ordinary first-run work.

**Promote when**

The team can explain the sandbox/approval model, recover a bad local change, and distinguish model advice from execution authority.

## Level 2 — structured delegated work

**Prerequisites**

Everything from Level 1, plus canonical Projects, Work Items, ownership/handoff conventions and optional authoritative TaskSources.

**Capabilities**

- external task intake;
- canonical Work Item lifecycle;
- queues, blockers, handoffs and reconciliation;
- bounded delegated implementation/validation.

**Primary risks**

- shadow work state between codex-web and a provider;
- stale/duplicate events;
- unclear ownership.

**Observable success**

- provider records project deterministically into canonical work;
- blocked work names an owner/next action;
- duplicate/stale intake does not create duplicate execution.

**Promote when**

Teams can trace one provider issue through Work Item state, execution, handoff and completion without relying on chat history.

## Level 3 — governed external actions

**Prerequisites**

Everything from Level 2, plus canonical identities/Roles, Resources, SecretReferences, ActionProviders/ActionIntents, isolated workers, ApprovalRequests and Evidence.

**Capabilities**

- controlled provider mutations;
- step-up/approval for high-risk actions;
- isolated execution;
- provider receipts and verification;
- rollback/reconciliation where supported.

**Primary risks**

- excessive credential scope;
- unsafe retries;
- action against the wrong Resource;
- assuming provider success without verification.

**Observable success**

- every external action has an ActionIntent;
- raw credentials are absent from prompts/state;
- authority/security decisions are inspectable;
- unknown outcomes reconcile instead of blindly replaying.

**Promote when**

Operators can use Explain Action to identify trigger, Role/policy, Resource scope, provider receipt, Evidence and result.

## Level 4 — event-driven bounded automation

**Prerequisites**

Everything from Level 3, plus canonical scheduling/events, Attention, evaluations/replay, budgets and tested provider capacity behavior.

**Capabilities**

- event/schedule-driven work;
- deterministic routing before reasoning;
- bounded model/token/cost/action budgets;
- automatic low-risk preparation/execution under policy;
- Attention escalation for human intervention.

**Primary risks**

- feedback/retry storms;
- reasoning loops;
- provider throttling;
- missed human escalation.

**Observable success**

- idle use consumes approximately zero model tokens;
- replay/evaluation is reproducible;
- load-shed/circuit behavior is visible;
- human-needed states reach Attention.

**Promote when**

The team has exercised both success and intentionally blocked/throttled/expired scenarios and can recover them without bypassing policy.

## Level 5 — controlled production autonomy

**Prerequisites**

Everything from Level 4, plus production qualification Evidence for release, incident readiness, recovery, capacity, upgrade compatibility, worker plane and audit integrity as required by policy.

**Capabilities**

- bounded autonomous production changes;
- immutable release promotion;
- incident containment/recovery;
- replicated control-plane ownership where supported;
- recovery drills and upgrade orchestration;
- scoped pause/kill and break-glass controls.

**Primary risks**

- larger blast radius;
- correlated infrastructure/provider failure;
- unsafe version skew;
- recovery/rollback assumptions;
- compromised audit integrity.

**Observable success**

- Autonomy Control Center reports qualification from canonical Evidence;
- operators can pause globally or by supported scope;
- production actions explain their blast radius;
- restore drills meet RPO/RTO;
- capacity tests demonstrate bounded overload behavior;
- rollback claims remain truthful across upgrades.

**Promotion criteria**

There is no required Level 6. Increasing action scope or autonomy beyond a tested Level 5 policy is a new risk decision and should require fresh qualification.

## Existing organizations: migrate one boundary at a time

A practical sequence is:

1. keep existing source control/CI/monitoring/IAM as authoritative systems;
2. introduce codex-web for supervised developer work;
3. project one team/project into canonical Work Items;
4. add one external TaskSource;
5. add one low-risk ActionProvider/Resource with explicit verification;
6. exercise approvals, unknown outcomes and reconciliation;
7. add bounded scheduled/event-driven work;
8. run evaluation/capacity/recovery/upgrade drills;
9. enable only the production-autonomy scopes that have qualifying Evidence.

Do not migrate every tool or team at once. Provider-neutral contracts are intended to let codex-web coordinate existing systems, not replace them.
