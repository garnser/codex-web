from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_web.models import (
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    TaskSourceIdentity,
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
    resource_ids_for_project: Callable[..., list[str]]
    leading_owner_cue_in_action: Callable[[str | None], str | None]
    default_validation_owner: str
    default_release_owner: str
    non_implementation_owners: frozenset[str]
    get_state: Callable[[str], WorkItemState | None] | None = None
    get_state_by_source_identity: Callable[
        [TaskSourceIdentity],
        WorkItemState | None,
    ] | None = None
    save_state: Callable[[WorkItemState], None] | None = None

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
            get_state=getattr(
                host,
                "_get_work_item_state_record",
                None,
            ),
            get_state_by_source_identity=getattr(
                host,
                "_get_work_item_state_by_source_identity",
                None,
            ),
            save_state=getattr(
                host,
                "_put_work_item_state_record",
                None,
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


OWNER_QUEUE_AGENTS = (
    "james",
    "carl",
    "dana",
    "quinn",
    "riley",
    "nora",
    "larry",
    "tom",
    "janice",
    "maya",
    "sally",
)
HANDOFF_COORDINATION_CHANNEL = "C0B9M89AHCY"
DEFAULT_VALIDATION_OWNER = "quinn"
DEFAULT_RELEASE_OWNER = "release manager"
NON_IMPLEMENTATION_OWNERS = frozenset(
    {
        DEFAULT_VALIDATION_OWNER,
        DEFAULT_RELEASE_OWNER,
        "orchestrator",
        "carl",
        "compliance manager",
        "nora",
        "maya",
        "larry",
    }
)


def default_leading_owner_cue(next_action: str | None) -> str | None:
    return leading_owner_cue(
        next_action,
        owner_candidates=OWNER_QUEUE_AGENTS + (DEFAULT_RELEASE_OWNER,),
    )


def gitlab_group_path(
    settings: GitLabProjectRoutingSettings,
) -> str | None:
    for path in settings.project_paths:
        normalized = (path or "").strip().strip("/")
        if normalized:
            return normalized.split("/", 1)[0]
    return None


def gitlab_label_names(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            labels.append(value)
        elif isinstance(value, dict):
            name = value.get("title") or value.get("name")
            if name:
                labels.append(str(name))

    attrs = payload.get("object_attributes") or {}
    changes = payload.get("changes") or {}
    for source in (
        payload.get("labels"),
        attrs.get("labels"),
        (changes.get("labels") or {}).get("current"),
    ):
        if isinstance(source, list):
            for item in source:
                add(item)
    for key in ("labels", "label_names"):
        source = attrs.get(key)
        if isinstance(source, list):
            for item in source:
                add(item)
    return sorted(
        {
            label.strip()
            for label in labels
            if label and label.strip()
        }
    )


def gitlab_owner_agents(
    payload: dict[str, Any],
    settings: GitLabProjectRoutingSettings,
) -> list[str]:
    owners: list[str] = []
    for label in gitlab_label_names(payload):
        match = re.match(
            r"owner::(.+)",
            label.strip(),
            re.IGNORECASE,
        )
        if match:
            owners.append(match.group(1).strip().lower())
    if owners:
        return sorted(set(owners))
    kind = str(
        payload.get("object_kind")
        or payload.get("event_name")
        or ""
    ).lower()
    return settings.fallback_agents_by_kind.get(kind, [])


def gitlab_url(payload: dict[str, Any]) -> str | None:
    attrs = payload.get("object_attributes") or {}
    return (
        attrs.get("url")
        or attrs.get("web_url")
        or (payload.get("project") or {}).get("web_url")
    )


def gitlab_project_issue_ref(
    payload: dict[str, Any],
) -> str | None:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    project_path = str(
        project.get("path_with_namespace") or ""
    ).strip()
    iid = attrs.get("iid")
    if not project_path or iid in {None, ""}:
        return None
    return f"{project_path}#{iid}"


def gitlab_mr_refs_from_payload(
    payload: dict[str, Any],
) -> list[str]:
    refs: list[str] = []
    attrs = payload.get("object_attributes") or {}
    references = attrs.get("references")
    for candidate in (
        attrs.get("source_branch"),
        attrs.get("target_branch"),
        (
            references.get("full")
            if isinstance(references, dict)
            else None
        ),
    ):
        if isinstance(candidate, str) and candidate.strip():
            refs.append(candidate.strip())
    changes = payload.get("changes") or {}
    for source in (
        changes.get("description"),
        changes.get("title"),
    ):
        if isinstance(source, dict):
            for value in source.values():
                if isinstance(value, str):
                    refs.extend(
                        re.findall(
                            r"[A-Za-z0-9._-]+![0-9]+",
                            value,
                        )
                    )
    return sorted(set(refs))


def gitlab_token_for_project(
    project_id: str,
    *,
    project_lookup: Callable[[str], Any],
) -> str | None:
    env_token = (
        os.environ.get("CODEX_WEB_GITLAB_TOKEN") or ""
    ).strip()
    if env_token:
        return env_token
    with contextlib.suppress(Exception):
        secrets_path = (
            Path(project_lookup(project_id).path)
            / "CODEX-SECRETS.md"
        )
        section = False
        for line in secrets_path.read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("### "):
                if line.strip() == "### GitLab codexops":
                    section = True
                    continue
                if section:
                    break
            if section and line.startswith("- Token: "):
                token = line.split(": ", 1)[1].strip()
                if token:
                    return token
    return None
