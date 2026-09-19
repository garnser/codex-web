# Platform administration

This guide is the operator-facing map of privileged codex-web state. The
architecture documents define contracts; this guide explains which canonical
surface owns a setting, what an administrator may change, and what must never be
treated as ordinary configuration.

## Organizations and workspaces

Organizations are tenant boundaries. Workspaces are operating scopes inside an
organization. Identities, policies, resources, secrets, definitions, provider
bindings, work, evidence and automation are always interpreted inside their
canonical tenant/workspace scope.

Before changing privileged state:

1. confirm the active organization/workspace;
2. confirm the acting human/service identity;
3. confirm required MFA/step-up assurance;
4. inspect the target object's current canonical revision/state;
5. preview affected projects/resources when the surface supports it.

Never infer tenant scope from a repository path, issue text or model output.

See [Identity and tenancy](../architecture/identity-tenancy.md).

## Human identities, sessions, MFA, service identities and SSO

Human sessions authenticate a person. Service identities authenticate a
non-human workload. Roles and authority are separate from authentication.

Administrative rules:

- use MFA/step-up for privileged mutation;
- scope service tokens to the minimum supported service scopes;
- revoke compromised/unused sessions and tokens rather than reusing them;
- treat SSO/IdP membership as identity input, not as an implicit grant of every
  codex-web role;
- preserve separation of duties for approvals that require distinct humans.

A valid session does not prove that an actor may perform an action. Canonical
Role/authority and policy evaluation remains authoritative.

## Secrets, credentials and encryption keys

Secrets are stored and referenced through the canonical secret boundary.
Provider/action/runtime state should contain a secret reference, never the raw
credential.

Cryptographic key material is a separate boundary:

- keys have purpose, backend, ID and version metadata;
- plaintext key material is never rendered by the admin UI or copied into
  ordinary configuration;
- rotation/recovery must preserve references required by encrypted historical
  state and backups;
- removing/revoking a key version can make protected historical data
  intentionally unrecoverable, so dependency checks are required first.

See [Trust and credentials](trust-and-credentials.md),
[Secrets](../architecture/secrets.md), and
[Encryption key management](../architecture/encryption-key-management.md).

## Definitions, configuration, policy, entitlements and canonical state

Keep these concepts distinct:

| Concept | Purpose | Typical mutation path |
| --- | --- | --- |
| Definition | Versioned reusable operational/domain description | draft → validate → publish → supersede/rollback |
| Configuration | Effective runtime/deployment values and rollout flags | typed configuration API/admin surface |
| Policy | Authorization, approvals, limits and permitted behavior | canonical policy/Definition surface |
| Secret/key reference | Protected credential/cryptographic boundary | secret/key admin API |
| Entitlement/quota | Service capability and consumption limit | entitlement/quota admin surface |
| Canonical state | Current operational/domain facts | owning domain API only |

Do not create a second UI-owned copy of any of these.

See [Definition Registry administration](definition-registry.md) and
[Configuration](../architecture/configuration.md).

## Resources

Resources describe governed targets that external actions may affect. Confirm:

- tenant/workspace ownership;
- resource type/provider identity;
- risk/classification metadata;
- active bindings and credential references;
- current authority/policy implications.

High-impact mutations should show resource scope/blast radius before execution.

See [Resource Catalog](../architecture/resource-catalog.md).

## Task, action, model and agent providers

Provider configuration has two layers:

1. canonical codex-web binding/configuration/authority state;
2. provider-owned external state.

Keep them visibly separate. A successful configuration write does not prove the
external provider accepted an action. ActionIntents, receipts and verification
provide that proof.

For model providers, configure routing and prompt revisions through the
ModelGateway; do not hard-code model choice in feature UI.

For execution agents, AgentProvider/AgentRuntime/AgentSession selection remains
bounded by canonical capabilities, assignments and worker policy.

See:

- [TaskSource contract](../architecture/task-source-contract.md)
- [ActionProvider contract](../architecture/action-providers.md)
- [ModelGateway](../architecture/model-gateway.md)
- [Agent providers](../architecture/agent-providers.md)

## Execution workers and isolated execution

The control plane decides what is allowed; execution workers perform bounded
untrusted work.

Administrators should inspect:

- worker identity, version, pool and lifecycle;
- supported execution-contract versions/capabilities;
- current assignments, leases and fencing tokens;
- sandbox/network/resource-limit policy;
- quarantine/drain state.

A worker that cannot satisfy the exact execution contract must reject the
assignment rather than guess compatibility.

See [Execution worker boundary](../architecture/execution-worker-boundary.md)
and [Execution workspaces](../architecture/execution-workspaces.md).

## Extensions and plugins

Installation, enablement and permission grant are separate operations.
Extensions may not self-authorize.

Before enabling/upgrading an extension verify:

- manifest ID/version and compatibility range;
- provenance/signature/digest state;
- requested vs granted capabilities;
- configuration schema/migrations;
- lifecycle/quarantine state;
- host/runtime compatibility.

See [Extension developer guide](../extensions/developer-guide.md) and
[Extensions architecture](../architecture/extensions.md).

## Roles, contracts, rulesets, budgets and approvals

Mutable role/execution/ruleset definitions belong in the Definition Registry.
Runtime enforcement must retain the exact revision used.

For approval-backed actions inspect:

- exact target/version/digest;
- policy/authority reason;
- required assurance and quorum;
- distinct-human/separation-of-duties requirements;
- expiry and consumption state;
- resulting operation reference.

Never approve from a copied chat summary when the canonical ApprovalRequest is
available.

## Goals, Decisions, Metrics and Executive roles

Goals describe outcomes. Decisions record choices/provenance. Metrics provide
measured observations. Executive output is advisory until materialized through
canonical Goal/Decision/Work/Action boundaries.

Do not mark a Goal complete solely because a task or agent says it succeeded;
verify the measured outcome.

## Autonomy administration

Autonomy level, budgets, qualification gates, scoped pauses, dry-run/simulation
and break-glass are canonical policy/runtime state.

Before increasing autonomy:

- inspect required qualification Evidence;
- verify Recovery, Capacity, Release, Upgrade, execution-plane and Audit gates
  required by policy;
- exercise dry-run/simulation;
- verify global and scoped pause controls;
- confirm blast radius for representative high-impact ActionIntents.

The UI must report canonical blockers; it must not invent a client-side
eligibility calculation.

## Recommended baseline

For a new production-capable workspace:

- MFA for human administrators;
- least-privilege service identities;
- external credentials stored by reference;
- managed encryption/backup keys with recovery ownership;
- Resources classified before provider actions;
- published versioned Definitions;
- isolated execution workers with explicit network/resource limits;
- canonical approvals for high/critical-risk actions;
- Evidence/verification for completion/release/recovery;
- regular restore, capacity and upgrade drills;
- audit-integrity verification and operator Attention routing;
- autonomy initially paused or bounded until qualification is complete.
