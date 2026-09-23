# Multi-repository Projects: setup, execution, migration, and recovery

Use this guide when one Codex Web Project contains more than one repository, when a task needs read-only context from sibling repositories, or when an existing shared/native workspace must be converted to canonical isolated execution.

The safe operating model is simple: a Project owns canonical Resource bindings; each mutating execution has exactly one mutable repository target; optional sibling repositories are explicit read-only context; execution happens in an isolated ExecutionWorkspace; worker, sandbox, network, credential, lease, and authority checks happen before execution; and repository/task/model content is data, never authority.

## Prerequisites

Before enabling execution, verify the active organization/workspace, the Project path, canonical repository Resources and Project bindings, worker capabilities, SecretReferences, supported sandbox/profile, and Project execution readiness.

Do not use a host path, repository text, task text, or model response as a substitute for canonical Resource binding.

## Repository topology

### One mutable repository

When a Project has exactly one active repository Resource, Codex Web may use the single-repository compatibility fallback as the deterministic mutable target.

Projects persist one repository-selection policy:

- `deterministic` — the existing/default behavior. Project readiness requires a Project-level target to resolve deterministically before execution.
- `explicit` — valid multi-repository topology may have no Project-wide default. Project readiness reports `repository_target_required_per_turn` and remains non-blocking as long as active repository Resources exist. Each executable turn must later provide enough canonical context to resolve one mutable repository.
- `coordinated` — the complete active repository set bound to the Project is fixed as the writable scope before each repository execution. This is an explicit Project-level opt-in for workspaces whose normal unit of work spans repositories; turns do not repeat repository selection, and canonical preflight still validates every member before mutation.

Bootstrap `repositorySelection: single` and `repositorySelection: default` map to the deterministic Project policy. Bootstrap `repositorySelection: explicit` and `repositorySelection: coordinated` map directly to their corresponding Project policies.

For deterministic multi-repository Projects, make the mutable target resolvable through one of the canonical sources, in precedence order:

1. repository Resource attached to the Work Item;
2. explicit repository selection for the thread/turn;
3. thread execution-profile repository binding;
4. Project execution-default routing binding.

If more than one repository remains eligible and no canonical target resolves the ambiguity, execution stops with `repository_target_ambiguous`. Do not choose the first filesystem directory and do not ask a model to decide authority.

Changing an existing Project policy is an explicit administrator configuration action through the Project repository-selection API or ProjectBootstrap reconciliation. It does not add/remove Resource bindings or infer authority from filesystem paths.

### Contextual target convergence

For an `explicit` Project, repository authority is resolved during turn preflight before any ExecutionWorkspace, lease, assignment, credential use, or provider mutation.

Canonical mutable-target selectors are validated independently:

1. Work Item repository Resource;
2. explicit turn repository selection;
3. thread/execution-profile repository binding;
4. authorized routing/default repository.

If several selectors are present, they must resolve to the same active Project-bound repository. Matching selectors converge and their provenance is retained on the canonical `RepositoryExecutionTarget.selection_evidence`.

Contradictory selectors fail closed with `repository_target_conflict`; precedence never hides contradictory authority. Missing contextual selection under an explicit Project policy fails with `repository_target_missing`. Wrong-tenant, wrong-type, inactive, or unbound targets fail before execution with a typed repository-target blocker.

After repository selection succeeds, all remaining Project readiness checks still apply, including worker capability, sandbox/profile, SecretReference, migration, and workspace prerequisites.

### Read-only sibling repositories

Add sibling repositories only when the task needs them. They remain separate canonical Resources and are provisioned as read-only execution members below /mnt/codex-context/<resource-id>.

They stay read-only even when the mutable repository uses danger-full-access. The Project root is not mounted as a compatibility shortcut.

## Execution profiles

### Repository execution

A repository execution profile requires one mutable repository target and the worker capabilities required by the profile. The resulting assignment records Project and Resource scope, target provenance, mutable/read-only repository IDs, profile revision, sandbox/approval policy, worker contract, workspace/lease identity, and SecretReferences rather than raw credentials.

### Orchestration-only execution

Use orchestration-only for control-plane coordination that does not need a Git checkout. It receives a scratch ExecutionWorkspace and no repository mutation authority.

Control-plane operations are available only through the assignment-bound broker. The broker exposes an allowlisted API surface, applies tenant/project/resource authority, validates the current worker/fence, records correlation/audit data, and exposes no reusable administrator credential.

Arbitrary proxying to localhost, private control-plane endpoints, or the open network is not part of the broker contract.

## Sandbox authority

Sandbox mode changes execution behavior; it does not replace canonical authorization.

### read-only

The mutable execution workspace is mounted read-only. Repository mutation is not authorized.

### workspace-write

The selected mutable repository workspace is writable. Sibling repository context remains read-only. Host-wide filesystem access is not granted.

### danger-full-access

danger-full-access means the assigned mutable execution workspace is writable without the narrower workspace-write restrictions that apply inside that worker boundary. It does not mean host or control-plane administrator access.

It does not bypass the canonical repository target, read-only sibling mounts, tenant/project/resource scope, ExecutionWorkspace leases/fencing, worker readiness, network checks, SecretReference boundaries, approval/authority rules, broker allowlists, or the prohibition on broad host/root mounts.

The built-in Bubblewrap worker still uses a private network namespace and does not mount / as a compatibility root.

A legacy environment that historically treated danger-full-access as broader host authority is not semantically equivalent to the current contained mode. Migration requires explicit administrator approval for that material authority conversion.

## Verify readiness

Use three distinct health layers:

- /api/livez — process liveness only;
- /api/readyz — application/runtime readiness;
- Project readiness — semantic/execution readiness for the selected Project.

For worker-specific diagnosis inspect GET /api/execution-workers/readiness.

A healthy process or codexReady: true does not prove that a Project has a repository target, compatible worker, valid SecretReference, supported sandbox, or available execution capacity.

## Preflight blockers

Structural/policy failures are retained as blocked preflight attempts rather than disappearing as inactive turns. The thread view preserves the submitted message and shows effective execution context, correlation ID, typed blocker, target, remediation, and status.

Common blocker codes include repository_target_missing, repository_target_ambiguous, repository_target_unauthorized, execution_profile_incompatible, sandbox_profile_unsupported, network_policy_unsupported, worker_capability_missing, credential_reference_missing, workspace_provisioning_blocked, lease_conflict, quota_or_capacity_blocked, and control_plane_scope_missing.

Retryable provider capacity may enter a visible queue instead of becoming a structural blocker.

### Retry after remediation

1. fix the canonical blocker rather than editing retained state;
2. re-check readiness or the referenced Resource/worker/workspace surface;
3. use the retained attempt Retry action if authorized;
4. verify the attempt moves to started or reaches a new typed blocker.

Retry reuses the original execution/correlation identity. Repeated clicks are deduplicated. After a crash, an already-active execution with that identity is recognized rather than dispatched again.

## Inspect an ExecutionWorkspace

For a mutable multi-repository execution verify exactly one repository member has write access, sibling members have read access, the mutable path is under the private execution-workspace root, sibling sandbox paths are /mnt/codex-context/<resource-id>, lease/resource IDs match the assignment, revisions and branch identity are present, and no Project-root or host-root compatibility mount exists.

Useful APIs include GET /api/execution-workspaces, GET /api/execution-workspaces/{workspace_id}, GET /api/execution-workspaces/{workspace_id}/events, POST /api/execution-workspaces/{workspace_id}/renew, POST /api/execution-workspaces/{workspace_id}/release, and POST /api/execution-workspaces/recover.

## Migrate an existing shared/native layout

Use the legacy Project migration workflow rather than hand-editing Resources or thread settings.

1. back up deployment state;
2. run migration dry-run;
3. inspect discovered repositories and proposed per-thread targets/profiles;
4. resolve ambiguous repositories explicitly;
5. review any sandbox/profile authority difference;
6. approve material authority conversion only when intended;
7. apply the plan;
8. if interrupted, resume the same idempotent plan;
9. inspect migration status and Project readiness;
10. run a bounded verification turn before broader use.

Migration preserves native thread identity/history and bot bindings. Migrated repository/thread content cannot grant repository, sandbox, broker, credential, or administrative authority.

Compatibility path mappings are scoped migration metadata. They expire at a concrete time, may be revoked early by an administrator through the Project-scoped compatibility endpoint, retain revocation provenance, and do not bypass canonical execution checks.

See [Legacy Project migration](legacy-project-migration.md).

## Rootless Podman

Rootless Podman is qualified for the control plane. Nested Bubblewrap local command execution inside that rootless container is not currently a supported worker boundary.

Do not recover by adding privileged mode, host CAP_SYS_ADMIN, a container socket, or broad host filesystem mounts. Place execution on a qualified native Linux worker or dedicated VM/worker boundary instead.

See [Rootless Podman execution-worker qualification](rootless-podman.md).

## Failure and recovery

For repository target blockers, repair canonical Project Resource bindings or target provenance. For worker/sandbox/network blockers, move to a qualified worker/profile rather than advertising unsupported capabilities. For lease conflicts, reconcile the canonical lease/fence. For workspace provisioning failures, repair the backend/path; provisioning failure releases the reservation and creates no assignment.

For unknown external provider outcomes, reconcile canonical ActionIntent/receipt/correlation data before retrying. A timeout is not proof of failure.

## Rollback boundary

Migration dry-run is non-mutating and can be discarded. After apply or execution creates canonical Resources, bindings, workspaces, branches, or thread settings, rollback is compensating rather than a transactional restoration of the old filesystem layout.

Preserve native thread IDs/history, audit/evidence records, branches with unmerged work, migration reports, and compatibility-window timestamps. Use explicit release/cleanup/compensating operations instead of deleting canonical audit history.

## Verification checklist

A qualified multi-repository Project demonstrates deterministic repository selection, one mutable checkout per mutation, read-only sibling context, safe concurrency, orchestration-only execution without Git authority, allowlisted/fenced/audited broker operations, no raw admin credentials, fail-closed symlink/path escapes, durable/idempotent preflight retry, resumable migration without duplicate Resources, truthful compatibility expiry, and fail-closed rootless-Podman execution readiness.

See [Multi-repository security qualification](multi-repository-security-qualification.md) for the automated evidence matrix.
