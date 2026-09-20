from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from codex_web.models import GitLabRoutingSettings, WorkItemState


@dataclass(frozen=True, slots=True)
class GitLabRoutingDependencies:
    load_settings: Callable[[], GitLabRoutingSettings]
    normalize_strings: Callable[[Any], list[str]]
    binding_for_agent: Callable[[str, str], Any | None]
    preferred_agent_conversations: Callable[
        [str, str, list[str]], list[str]
    ]
    clone_binding_to_known_channel: Callable[[Any, str], Any]
    master_binding: Callable[[str], Any | None]


@dataclass(frozen=True, slots=True)
class GitLabWorkItemRuntimeDependencies:
    split_brain_findings: Callable[[WorkItemState], list[str]]
    coerce_owner: Callable[[str | None], str | None]
    project_event: Callable[..., Any]
    project_lookup: Callable[[str], Any]
    load_projects: Callable[[], list[Any]]


@dataclass(frozen=True, slots=True)
class GitLabOperationalDependencies:
    api_base_url: str
    load_support_state: Callable[[], dict[str, Any]]
    save_support_state: Callable[[dict[str, Any]], None]
    load_semantic_events: Callable[[], dict[str, float]]
    save_semantic_events: Callable[[dict[str, float]], None]
    verify_webhook: Callable[[Any], None]
    append_event: Callable[[dict[str, Any]], None]
    publish_event: Callable[[dict[str, Any]], Awaitable[Any]]
    truncate_text: Callable[[Any, int], str]
    dispatch_event: Callable[[Any, str, str], Awaitable[dict[str, Any]]]
    format_event_prompt: Callable[[dict[str, Any], str | None], str]
    send_event_notice: Callable[
        [Any, dict[str, Any], str | None, dict[str, Any]],
        Awaitable[dict[str, Any]],
    ]
    schedule_recovery: Callable[..., Any]
