from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


async def _async_noop_dispatch(
    _binding: Any,
    _text: str,
    _source: str,
) -> dict[str, Any]:
    return {"ok": False, "queued": False}


@dataclass(frozen=True, slots=True)
class AutonomyRuntimeDependencies:
    """Narrow runtime collaborators used by autonomous watchdog cycles."""

    load_gitlab_routing_settings: Callable[[], Any]
    load_work_item_states: Callable[[], dict[str, Any]]
    save_work_item_states: Callable[[dict[str, Any]], None]
    gitlab_token_for_project: Callable[[str], str | None]
    gitlab_group_path: Callable[[Any], str | None]
    gitlab_group_issues: Callable[..., Any]
    append_bot_event: Callable[[dict[str, Any]], None]
    append_work_item_event: Callable[[dict[str, Any]], None]
    work_item_event: Callable[..., dict[str, Any]]
    archive_active_handoff: Callable[..., Any]
    coerce_owner: Callable[[str | None], str | None]
    owner_queue_agents: tuple[str, ...]
    handoff_coordination_channel: str | None
    binding_for_agent: Callable[..., Any]
    orchestrator_binding: Callable[[str], Any | None]
    binding_prefix: Callable[[Any], str | None]
    replace_nonperforming_thread: Callable[..., Any]
    dispatch_event: Callable[..., Any]
    release_stale_active_turn: Callable[[str | None, str], Any]
    thread_is_active: Callable[[str | None], bool]
    thread_queue_depth: Callable[[str | None], int]
    thread_recently_active: Callable[[str | None], bool]
    watchdog_dispatch_allowed: Callable[[str], bool]
    record_watchdog_dispatch: Callable[[str], None]
    handoff_timeout_seconds: Callable[[], float]
    release_validation_sla_seconds: Callable[[], float]
    work_item_sla_threshold_seconds: Callable[[Any], float]
    owner_activity_timestamp: Callable[[Any], float]
    orchestrator_watchdog_candidates: Callable[[str], list[Any]]
    split_brain_watchdog_candidates: Callable[[str], list[Any]]
    format_orchestrator_watchdog_prompt: Callable[..., str]
    format_split_brain_watchdog_prompt: Callable[..., str]
    work_item_dispatch_text: Callable[[Any], str]

    @classmethod
    def from_host(cls, host: Any) -> "AutonomyRuntimeDependencies":
        return cls(
            load_gitlab_routing_settings=getattr(
                host,
                "_load_gitlab_routing_settings",
                lambda: type(
                    "_Routing",
                    (),
                    {"enabled": False, "projects": {}},
                )(),
            ),
            load_work_item_states=getattr(
                host,
                "_load_work_item_states",
                lambda: {},
            ),
            save_work_item_states=getattr(
                host,
                "_save_work_item_states",
                lambda _states: None,
            ),
            gitlab_token_for_project=getattr(
                host,
                "_gitlab_token_for_project",
                lambda _project_id: None,
            ),
            gitlab_group_path=getattr(
                host,
                "_gitlab_group_path",
                lambda _settings: None,
            ),
            gitlab_group_issues=getattr(
                host,
                "_gitlab_group_issues",
                lambda *_args, **_kwargs: [],
            ),
            append_bot_event=getattr(
                host,
                "_append_bot_event",
                lambda _event: None,
            ),
            append_work_item_event=getattr(
                host,
                "_append_work_item_event",
                lambda _event: None,
            ),
            work_item_event=getattr(
                host,
                "_work_item_event",
                lambda ref, event_type, **kwargs: {
                    "ref": ref,
                    "event_type": event_type,
                    **kwargs,
                },
            ),
            archive_active_handoff=getattr(
                host,
                "_archive_active_handoff",
                lambda state, **_kwargs: state,
            ),
            coerce_owner=getattr(
                host,
                "_coerce_owner",
                lambda value: (
                    str(value).strip().lower() if value else None
                ),
            ),
            owner_queue_agents=tuple(
                getattr(host, "OWNER_QUEUE_AGENTS", ())
            ),
            handoff_coordination_channel=getattr(
                host,
                "HANDOFF_COORDINATION_CHANNEL",
                None,
            ),
            binding_for_agent=getattr(
                host,
                "_binding_for_agent",
                lambda *_args, **_kwargs: None,
            ),
            orchestrator_binding=getattr(
                host,
                "_orchestrator_binding",
                lambda _project_id: None,
            ),
            binding_prefix=getattr(
                host,
                "_binding_prefix",
                lambda _binding: None,
            ),
            replace_nonperforming_thread=getattr(
                host,
                "_replace_nonperforming_thread_if_needed",
                lambda binding, _reason: binding,
            ),
            dispatch_event=getattr(
                host,
                "_dispatch_event_to_binding",
                _async_noop_dispatch,
            ),
            release_stale_active_turn=getattr(
                host,
                "_release_stale_active_turn",
                lambda _thread_id, _reason: None,
            ),
            thread_is_active=getattr(
                host,
                "_thread_is_active",
                lambda _thread_id: False,
            ),
            thread_queue_depth=getattr(
                host,
                "_thread_queue_depth",
                lambda _thread_id: 0,
            ),
            thread_recently_active=getattr(
                host,
                "_thread_recently_active",
                lambda _thread_id: False,
            ),
            watchdog_dispatch_allowed=getattr(
                host,
                "_watchdog_dispatch_allowed",
                lambda _key: True,
            ),
            record_watchdog_dispatch=getattr(
                host,
                "_record_watchdog_dispatch",
                lambda _key: None,
            ),
            handoff_timeout_seconds=getattr(
                host,
                "_work_item_handoff_timeout_seconds",
                lambda: 300.0,
            ),
            release_validation_sla_seconds=getattr(
                host,
                "_release_validation_sla_seconds",
                lambda: 900.0,
            ),
            work_item_sla_threshold_seconds=getattr(
                host,
                "_work_item_sla_threshold_seconds",
                lambda _state: 900.0,
            ),
            owner_activity_timestamp=getattr(
                host,
                "_owner_activity_timestamp",
                lambda state: getattr(
                    state,
                    "last_meaningful_update_at",
                    0.0,
                ),
            ),
            orchestrator_watchdog_candidates=getattr(
                host,
                "_orchestrator_watchdog_candidates",
                lambda _project_id: [],
            ),
            split_brain_watchdog_candidates=getattr(
                host,
                "_split_brain_watchdog_candidates",
                lambda _project_id: [],
            ),
            format_orchestrator_watchdog_prompt=getattr(
                host,
                "_format_orchestrator_watchdog_prompt",
                lambda _project_id, _items: "",
            ),
            format_split_brain_watchdog_prompt=getattr(
                host,
                "_format_split_brain_watchdog_prompt",
                lambda _project_id, _items: "",
            ),
            work_item_dispatch_text=getattr(
                host,
                "_work_item_dispatch_text",
                lambda state: str(getattr(state, "ref", "")),
            ),
        )
