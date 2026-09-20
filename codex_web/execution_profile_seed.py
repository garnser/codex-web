"""Bootstrap seed for canonical execution profiles."""
from __future__ import annotations

from codex_web.execution_profiles import (
    ExecutionProfileCatalogDefinition,
    ExecutionProfileContract,
)


def execution_profile_catalog_seed_payload() -> dict[str, object]:
    return ExecutionProfileCatalogDefinition(
        profiles=(
            ExecutionProfileContract(
                id="repository-write",
                name="Repository write",
                description=(
                    "Isolated repository execution with one mutable canonical repository "
                    "and optional read-only repository context."
                ),
                workspace_mode="repository",
                repository_access="mutable",
                required_worker_capabilities=("git", "command_execution"),
            ),
            ExecutionProfileContract(
                id="orchestration-only",
                name="Orchestration only",
                description=(
                    "Isolated scratch execution for coordination/control-plane work "
                    "without a Git worktree or repository mutation authority."
                ),
                workspace_mode="scratch",
                repository_access="none",
                required_worker_capabilities=("command_execution",),
                control_plane_operations=(
                    "project.read",
                    "resource.read",
                    "work_item.read",
                    "work_item.assign",
                    "work_item.handoff",
                    "work_item.steer",
                    "work_item.acknowledge",
                    "work_item.reconcile",
                    "work_item.retry",
                    "execution.read",
                    "evidence.read",
                ),
            ),
        ),
        default_profile_id="repository-write",
    ).model_dump(mode="json")
