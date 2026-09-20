# Multi-repository security qualification

This is the release/operator evidence map for the multi-repository execution and migration boundary. It connects each security claim to automated tests or CI qualification so operators do not infer safety from model output, screenshots, or implementation prose.

## Release gate

Changes to repository selection, execution workspaces, worker isolation, migration, brokered control-plane access, preflight/retry, or related UI are not qualified until the applicable repository CI jobs pass: unit-static, chromium, docker-smoke, distributed-backends, and rootless-podman-worker.

Screenshots document UX; they are not security evidence.

## Security evidence matrix

| Security claim | Automated evidence |
| --- | --- |
| Mutable executions do not silently share one checkout | tests/test_execution_workspaces.py parallel-worktree and conflicting-write lease tests |
| A multi-repository workspace has one mutable repository and explicit read-only siblings | test_multi_repository_workspace_provisions_mutable_and_read_only_members plus local-worker read-only mount tests |
| danger-full-access cannot make a read-only sibling writable | tests/test_local_execution_worker.py::test_danger_full_access_cannot_make_read_only_sibling_repository_writable |
| Host root is not implicitly mounted into local execution | local-worker namespace and danger-full-access boundary tests |
| Symlink/path tricks cannot escape the canonical Project root | test_relative_repository_alias_cannot_escape_project_root_through_symlink plus filesystem-boundary tests |
| Repository ambiguity fails closed instead of guessing | multi-repository binding ambiguity and migration ambiguity tests |
| Explicit unbound repository selection is unauthorized | test_unbound_explicit_repository_is_typed_as_unauthorized |
| Missing worker command capability blocks before workspace/assignment creation | test_missing_command_execution_blocks_before_workspace_creation |
| Sandbox/network/profile/credential blockers are distinguishable | typed binding tests plus test_network_profile_mismatch_is_structured_preflight_blocker |
| Provisioning, lease, and quota failures are typed and bounded | workspace provisioning/quota/lease binding tests and ExecutionWorkspace tests |
| Orchestration-only work has scratch state and no Git authority | test_orchestration_profile_uses_scratch_without_repository_or_git_authority and scratch-workspace tests |
| Broker access is allowlisted and cannot proxy arbitrary localhost | tests/test_control_plane_broker.py::test_non_allowlisted_arbitrary_localhost_and_method_mismatch_fail_closed |
| Broker operations are tenant/project scoped and fenced | test_project_scope_stale_fence_and_revoked_identity_fail_closed |
| Broker read/handoff uses canonical authority and audit | test_authorized_read_and_handoff_use_exact_role_authority |
| No reusable administrator credential is exposed | test_no_reusable_broker_or_admin_credential_is_exposed plus reference-only binding tests |
| Repository/task/model content cannot grant authority | security-boundary untrusted-task/model tests plus test_preserved_thread_content_cannot_grant_migration_authority |
| Legacy danger-full-access conversion is not silently equivalent | test_danger_full_access_requires_explicit_authority_approval |
| Legacy thread and bot binding identities survive migration | test_orchestrator_preserves_thread_and_bot_binding_without_git_target |
| Interrupted migration resumes without duplicate Resources | test_partial_apply_resumes_without_duplicate_resources |
| Compatibility mappings are observable, time-bounded, and removable | test_compatibility_mapping_expires_truthfully and test_compatibility_mapping_can_be_revoked_early_with_provenance |
| Retained structural blockers survive reload | tests/test_execution_preflight.py::test_blocked_attempt_survives_store_and_service_reload |
| Repeated retry clicks/replays do not duplicate execution | execution-preflight claim tests and TurnService retry tests |
| Crash recovery does not redispatch an already-active retained execution | TurnService recovered-active execution retry test |
| Browser shows retained message, context, blocker, remediation, correlation and retry | tests/browser/execution_preflight.spec.js |
| Tenant/resource isolation is enforced | ExecutionWorkspace tenant inspection, Resource Catalog isolation, broker project-scope tests |
| Stale leases/fences fail closed | ExecutionWorkspace expiry/conflict, worker lease/fence, and broker stale-fence tests |
| Rootless Podman does not escalate unsupported nested isolation | rootless-podman-worker CI job and local isolation readiness tests |

## End-to-end scenario coverage

Multi-repository execution tests prove deterministic target selection, one writable worktree, detached read-only sibling context, canonical assignment Resource IDs, resource-specific leases, cleanup, and read-only Bubblewrap mounting even under danger-full-access.

Orchestration tests prove scratch execution without Git authority. Broker tests exercise allowlisted read and governed handoff, authority evaluation, tenant/project scope, correlation/audit, stale fencing, and credential non-exposure.

Migration tests cover ambiguity, material danger-full-access authority conversion, partial apply/resume, native thread/bot-binding preservation, compatibility expiry, and compensating rollback boundaries.

Preflight tests cover structural blocker classification, durable persistence, immutable retained identity, retry authority, duplicate claims, stale-claim recovery, terminal status, and active-execution crash deduplication. Browser tests cover the operator remediation path.

Docker/distributed CI validate normal deployment/state backends. Rootless-Podman qualification has a positive non-root control-plane smoke and negative nested-Bubblewrap qualification without privileged fallback.

## Manual deployment evidence

Repository security properties are automated. Manual checks are limited to deployment facts the repository cannot know: intended host paths, upstream provider credential privilege, organization-specific authority definitions, and local backup/retention ownership.

Record deployment-specific checks in the operational Evidence/audit system. Never replace an automated fail-closed gate with a manual checkbox.

## Regression rules

When changing these boundaries, add or update a focused owning-layer test, preserve public contract identifiers, update remediation documentation when authority meaning changes, refresh sanitized UI/contextual-help fixtures for visible behavior, and require the full applicable CI matrix before release.

A green UI test alone does not qualify execution security. A green unit test alone does not qualify deployment/container behavior.
