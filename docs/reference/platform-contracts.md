# Platform contracts and reference map

Use the running OpenAPI document for exact endpoint schemas for the deployed
release. This reference maps major contract families to their durable
documentation and explains what compatibility guarantee operators/developers
should expect.

## Runtime and configuration

- container/runtime environment: [DOCKER.md](../../DOCKER.md) and
  [`.env.example`](../../.env.example);
- typed configuration and feature rollout:
  [Configuration architecture](../architecture/configuration.md);
- hosted capability/quotas: [Entitlements](../architecture/entitlements.md).

Environment variables are deployment inputs, not a substitute for canonical
mutable Definitions/Policy.

## Identity, security and data governance

- [Identity and tenancy](../architecture/identity-tenancy.md)
- [Security/trust boundaries](../architecture/security-trust-boundaries.md)
- [Secrets](../architecture/secrets.md)
- [Encryption/key management](../architecture/encryption-key-management.md)
- [Data governance](../architecture/data-governance.md)

Raw secret/key material is intentionally absent from ordinary API responses.

## Definitions, policy and authority

- [Definition Registry](../architecture/definition-registry.md)
- [Compatibility/versioning](../architecture/compatibility-versioning.md)
- [Role authority](../architecture/role-authority.md)
- [Execution contract schema](../architecture/execution-contract-schema.md)
- [ApprovalRequests](../architecture/approval-requests.md)

Persisted executions/audits should identify exact definition revisions.

## Work, events and orchestration

- [Work Item lifecycle](../architecture/work-item-lifecycle.md)
- [Work graphs](../architecture/work-graphs.md)
- [Canonical events](../architecture/canonical-events.md)
- [Scheduler](../architecture/durable-scheduler.md)
- [Orchestration inspector](../architecture/orchestration-inspector.md)
- [Attention](../architecture/attention.md)

Event/schedule contracts are versioned durable interfaces; consumers must reject
unsupported versions visibly.

## Providers and model/runtime contracts

- [TaskSource](../architecture/task-source-contract.md)
- [ActionProvider](../architecture/action-providers.md)
- [ActionIntent](../architecture/action-intents.md)
- [ModelGateway](../architecture/model-gateway.md)
- [Provider capacity](../architecture/provider-capacity.md)
- [Agent providers](../architecture/agent-providers.md)
- [Agent routing](../architecture/agent-routing.md)
- [Agent runtime telemetry](../architecture/agent-runtime-telemetry.md)

External provider state is not automatically canonical state. Receipts,
synchronization and reconciliation define the boundary.

## Worker and extension contracts

- [Execution worker boundary](../architecture/execution-worker-boundary.md)
- [Execution workspaces](../architecture/execution-workspaces.md)
- [Extension contract](../architecture/extension-contract.md)
- [Extensions](../architecture/extensions.md)
- [Extension developer guide](../extensions/developer-guide.md)

Incompatible worker/extension versions or contracts fail closed.

## Artifact, Evidence, release and audit contracts

- [Artifact content store](../architecture/artifact-content-store.md)
- [Artifact/Evidence](../architecture/artifact-evidence.md)
- [Releases](../architecture/releases.md)
- [Autonomy audit](../architecture/autonomy-audit.md)
- [Evaluation/replay](../architecture/autonomy-evaluation-replay.md)

Evidence is structured proof with lifecycle/verification semantics; model text is
not equivalent Evidence.

## Production operations compatibility

- [Storage scaling](../architecture/storage-scaling.md)
- [Capacity/resilience](../architecture/capacity-resilience.md)
- [Incidents](../architecture/incidents.md)
- [Recovery continuity](../architecture/recovery-continuity.md)
- [Safe upgrades](../architecture/safe-upgrades.md)

Supported version skew, restore compatibility and rollback boundaries are
explicit contracts. Never infer compatibility because two versions happen to
start successfully.

## Observability and correlation semantics

Use correlation/causation identifiers plus domain references such as
execution_id, ActionIntent ID, Work Item ref and provider receipt ID to trace a
flow. Structured logs/telemetry may be retained differently from canonical
audit/Evidence state; do not treat log retention as the audit retention policy.

See [Observability](../architecture/observability.md).

## API/event/schema compatibility rule

Public/admin APIs, canonical events, persisted domain schemas, provider/worker/
extension boundaries and Definitions must evolve under explicit compatibility
rules. An unsupported version should produce a visible incompatibility error,
not heuristic field guessing.

See [Documentation versioning](documentation-versioning.md).
