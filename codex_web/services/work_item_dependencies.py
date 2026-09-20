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
        data_dir = Path(getattr(host, "DATA_DIR", "."))
        events_file = Path(
            getattr(
                host,
                "WORK_ITEM_EVENTS_FILE",
                data_dir / "work_item_events.jsonl",
            )
        )
        return cls(
            data_dir=data_dir,
            events_file=events_file,
            load_states=getattr(
                host,
                "_load_work_item_states",
                lambda: {},
            ),
            save_states=getattr(
                host,
                "_save_work_item_states",
                lambda _states: None,
            ),
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
            default_validation_owner=getattr(
                host,
                "DEFAULT_VALIDATION_OWNER",
                "quinn",
            ),
            default_release_owner=getattr(
                host,
                "DEFAULT_RELEASE_OWNER",
                "release manager",
            ),
            non_implementation_owners=frozenset(
                getattr(
                    host,
                    "NON_IMPLEMENTATION_OWNERS",
                    {
                        "quinn",
                        "release manager",
                        "orchestrator",
                    },
                )
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
        def default_group_path(
            settings: GitLabProjectRoutingSettings,
        ) -> str | None:
            for path in settings.project_paths:
                normalized = (path or "").strip().strip("/")
                if normalized:
                    return normalized.split("/", 1)[0]
            return None

        return cls(
            api_base_url=getattr(
                host,
                "GITLAB_API_BASE",
                "https://gitlab.example/api/v4",
            ),
            token_for_project=getattr(
                host,
                "_gitlab_token_for_project",
                lambda _project_id: None,
            ),
            group_path=getattr(
                host,
                "_gitlab_group_path",
                default_group_path,
            ),
            load_routing_settings=getattr(
                host,
                "_load_gitlab_routing_settings",
                lambda: GitLabRoutingSettings(),
            ),
            project_issue_ref=getattr(
                host,
                "_project_issue_ref",
                lambda _payload: None,
            ),
            label_names=getattr(
                host,
                "_gitlab_label_names",
                lambda _payload: [],
            ),
            owner_agents=getattr(
                host,
                "_gitlab_owner_agents",
                lambda _payload, _settings: [],
            ),
            url=getattr(
                host,
                "_gitlab_url",
                lambda _payload: None,
            ),
            mr_refs_from_payload=getattr(
                host,
                "_mr_refs_from_payload",
                lambda _payload: [],
            ),
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
