from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_web.models import (
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    WorkItemState,
)


@dataclass(frozen=True, slots=True)
class WorkItemRuntimeDependencies:
    """Narrow canonical dependencies shared by work-item domain services."""

    data_dir: Path
    events_file: Path
    load_states: Callable[[], dict[str, WorkItemState]]
    save_states: Callable[[dict[str, WorkItemState]], None]
    load_projects: Callable[[], list[Any]]
    resource_ids_for_project: Callable[[str], list[str]]
    leading_owner_cue_in_action: Callable[[str | None], str | None]
    default_validation_owner: str
    default_release_owner: str
    non_implementation_owners: frozenset[str]

    @classmethod
    def from_host(cls, host: Any) -> "WorkItemRuntimeDependencies":
        return cls(
            data_dir=host.DATA_DIR,
            events_file=host.WORK_ITEM_EVENTS_FILE,
            load_states=host._load_work_item_states,
            save_states=host._save_work_item_states,
            load_projects=getattr(host, "_load_projects", lambda: []),
            resource_ids_for_project=getattr(
                host,
                "_resource_ids_for_project",
                lambda _project_id: [],
            ),
            leading_owner_cue_in_action=getattr(
                host,
                "_leading_owner_cue_in_action",
                lambda _next_action: None,
            ),
            default_validation_owner=host.DEFAULT_VALIDATION_OWNER,
            default_release_owner=host.DEFAULT_RELEASE_OWNER,
            non_implementation_owners=frozenset(
                host.NON_IMPLEMENTATION_OWNERS
            ),
        )


@dataclass(frozen=True, slots=True)
class GitLabWorkItemDependencies:
    """GitLab-only helpers required by canonical work-item projection."""

    api_base_url: str
    token_for_project: Callable[[str], str | None]
    group_path: Callable[[GitLabProjectRoutingSettings], str | None]
    load_routing_settings: Callable[[], GitLabRoutingSettings]
    project_issue_ref: Callable[[dict[str, Any]], str | None]
    label_names: Callable[[dict[str, Any]], list[str]]
    owner_agents: Callable[
        [dict[str, Any], GitLabProjectRoutingSettings],
        list[str],
    ]
    url: Callable[[dict[str, Any]], str | None]
    mr_refs_from_payload: Callable[[dict[str, Any]], list[str]]

    @classmethod
    def from_host(cls, host: Any) -> "GitLabWorkItemDependencies":
        return cls(
            api_base_url=host.GITLAB_API_BASE,
            token_for_project=host._gitlab_token_for_project,
            group_path=host._gitlab_group_path,
            load_routing_settings=host._load_gitlab_routing_settings,
            project_issue_ref=host._project_issue_ref,
            label_names=host._gitlab_label_names,
            owner_agents=host._gitlab_owner_agents,
            url=host._gitlab_url,
            mr_refs_from_payload=host._mr_refs_from_payload,
        )


def leading_owner_cue(
    next_action: str | None,
    *,
    owner_candidates: tuple[str, ...],
) -> str | None:
    action = (next_action or "").strip().lower()
    if not action:
        return None
    for owner in owner_candidates:
        normalized = owner.strip().lower()
        if action.startswith(f"{normalized}:"):
            return normalized
        if action.startswith(f"{normalized} "):
            return normalized
    return None
