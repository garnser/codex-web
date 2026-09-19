# Core Concepts

## The mental model

Codex-web is a control plane. It turns intent into governed work while keeping deterministic state and authority in application code.

```text
Goal / request / issue / event
            |
            v
   canonical scope + identity
            |
     policy + authority
            |
   project / work / decision
            |
  isolated agent execution
            |
      ActionIntent
       /      \
 approval    execute
       \      /
            v
 provider action / artifact
            |
     Evidence + verification
            |
       canonical result
            |
     reusable knowledge
```

## Canonical state vs provider state

**Canonical codex-web state** owns workflow identity, scope, policy, approvals, work lifecycle, ActionIntents, Evidence links, Goals, Decisions and other control-plane records.

**Provider state** remains authoritative for provider-owned facts: a GitLab issue, cloud deployment, Slack message, execution worker process or external model account is not made canonical merely by copying it into codex-web.

Reconciliation connects the two. If provider outcome is unknown, codex-web should record uncertainty and reconcile rather than invent success.

## Advisory vs execution authority

Models produce judgment and proposals. Authority comes from canonical identity, Role/policy, approvals, security/trust boundaries and execution contracts.

Content cannot grant authority. This includes repository text, prompts, webhook payloads, logs, retrieved memory, provider responses, extension output and model output.

## Organizations and workspaces

Organization/workspace scope is the primary tenant boundary. Domain objects, identities, policies and actions are evaluated inside that scope. Cross-tenant lookup must not be inferred from names or content.

## Projects and task sources

A Project binds work to a repository/workspace context. A project may configure one authoritative TaskSource. External systems are projected deterministically into canonical work rather than becoming a second work-state engine.

## Identities, roles and secrets

Identity says **who** acts. Roles/authority say **what they may do**. Secrets provide credential material only through canonical secret references/brokers; raw secrets do not belong in prompts, reusable definitions or ordinary domain state.

## Resources

The Resource Catalog gives actions a stable target identity and risk context. Provider-specific identifiers remain metadata around that canonical target.

## ActionProviders and ActionIntents

An ActionProvider describes external capabilities. An ActionIntent is the durable canonical record of a requested side effect, including authority/security decisions, resource scope, idempotency, retries, receipts and verification requirements.

High-impact work should be explainable from the stored ActionIntent chain rather than from chat history.

## Execution isolation

Agent reasoning and execution are separated from the control plane. Workers advertise supported capabilities/contracts and execute bounded assignments. Sandbox/network/credential boundaries are explicit.

## Artifacts and Evidence

Artifacts are durable outputs such as builds, reports or generated files. Evidence is structured proof—tests, scans, provider verification, policy evaluations or runtime results. Evidence should support completion/qualification decisions that would otherwise rely on assertion.

## Work, queues, handoffs and blockers

Canonical Work Items track lifecycle, ownership, handoffs, blockers and reconciliation. Queues are bounded delivery mechanisms, not a substitute for work state.

## Goals, graphs and Decisions

Goals state desired outcomes. Work graphs model dependencies. Decisions preserve rationale/provenance and can create governed work. These objects let codex-web trace why work exists rather than only what ticket requested it.

## Executive roles and organizational memory

Executive roles reason over authorized context and can delegate into canonical work. Organizational Memory stores governed reusable knowledge with provenance and lifecycle; it is not raw chat history.

## Approvals, budgets and autonomy

Approvals are canonical, version/digest-bound authorization records. Budgets bound actions, tokens, cost and production impact. Autonomy levels determine how far work may progress automatically, while ActionIntent/security/approval boundaries remain authoritative.

Read [Bounded autonomy](../architecture/bounded-autonomy.md) before enabling production autonomy.

## Next concepts

- [Identity and tenancy](../architecture/identity-tenancy.md)
- [Security trust boundaries](../architecture/security-trust-boundaries.md)
- [Role authority](../architecture/role-authority.md)
- [ActionIntents](../architecture/action-intents.md)
- [Execution worker boundary](../architecture/execution-worker-boundary.md)
- [Artifact and Evidence](../architecture/artifact-evidence.md)
