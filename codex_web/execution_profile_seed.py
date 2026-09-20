"""Bootstrap-only execution profile seed.

Runtime resolution uses the database-backed Definition Registry. These values
only establish the first published revision on clean/existing installations.
"""
from __future__ import annotations

from codex_web.execution_profile_models import (
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
    ExecutionRepositoryAccess,
    ExecutionWorkspaceMode,
)
from codex_web.execution_workers import WorkerCapability


SEED_EXECUTION_PROFILES = ExecutionProfileCatalogDefinition(
    profiles=(
        ExecutionProfileContract(
            id="repository-write",
            name="Repository write",
            description="Standard repository-scoped implementation execution.",
            workspace_mode=ExecutionWorkspaceMode.REPOSITORY,
            repository_access=ExecutionRepositoryAccess.WORKSPACE_WRITE,
            required_worker_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            allowed_sandboxes=(
                "workspace-write",
                "read-only",
                "danger-full-access",
            ),
            authority_explanation=(
                "Runs inside the canonical isolated repository workspace. "
                "Repository authority is limited to the selected canonical target."
            ),
        ),
        ExecutionProfileContract(
            id="orchestration-only",
            name="Orchestration only",
            description=(
                "Coordinates canonical work state without receiving a mutable "
                "repository checkout."
            ),
            workspace_mode=ExecutionWorkspaceMode.SCRATCH,
            repository_access=ExecutionRepositoryAccess.NONE,
            required_worker_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            allowed_sandboxes=("read-only",),
            allowed_control_plane_operations=(
                "work-items:read",
                "work-items:assign",
                "work-items:handoff",
                "work-items:acknowledge",
                "work-items:steer",
                "work-items:reconcile",
                "work-items:retry",
                "projects:read",
                "resources:read",
                "executions:read",
                "evidence:read",
            ),
            authority_explanation=(
                "Uses an isolated scratch workspace with no Git worktree and no "
                "repository mutation authority. Governed control-plane operations "
                "remain subject to canonical identity/policy and broker reachability."
            ),
        ),
    ),
    default_profile_id="repository-write",
    role_to_profile={
        "orchestrator": "orchestration-only",
    },
)


def execution_profile_catalog_seed_payload() -> dict:
    return SEED_EXECUTION_PROFILES.model_dump(mode="json")
