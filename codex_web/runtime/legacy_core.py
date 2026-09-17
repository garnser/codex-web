from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from codex_web.events import EventHub
from codex_web.storage.json_files import (
    atomic_write_text as _atomic_write_text,
    state_file_lock as _state_file_lock,
)
from codex_web.devhealth import build_context as build_devhealth_context, render_html as render_devhealth_html
from codex_web.devstatus import build_context as build_devstatus_context, render_html as render_devstatus_html
from codex_web.models import (
    ActiveThreadTurn,
    AgentChannelPresenceProjectSettings,
    AgentChannelPresenceSettings,
    ApprovalDecision,
    ApprovalSlackMessage,
    BotBinding,
    BotBindingCreate,
    BotConnection,
    BotConnectionCreate,
    BotInboundMessage,
    BotRouteTest,
    BotReplyTarget,
    BotThreadDetail,
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    IndexedThread,
    Project,
    ProjectCreate,
    QueuedTurn,
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRename,
    ThreadRunSettings,
    TurnCreate,
    WorkItemAckCreate,
    WorkItemEvent,
    WorkItemHandoff,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
    WorkItemState,
)
from codex_web.paths import (
    ACTIVE_TURNS_FILE,
    AGENT_CHANNEL_PRESENCE_FILE,
    APPROVAL_MESSAGES_FILE,
    BOT_DELIVERY_TARGETS_FILE,
    BOT_DETAILS_FILE,
    BOT_REPLY_TARGETS_FILE,
    BOTS_BINDINGS_FILE,
    BOTS_CONNECTIONS_FILE,
    BOTS_EVENTS_FILE,
    DATA_DIR,
    GITLAB_ROUTING_FILE,
    PROJECTS_FILE,
    SLACK_RELAY_NOTICE,
    SUPPORT_SERVICEDESK_STATE_FILE,
    SLACK_THREAD_ICONS_FILE,
    STATIC_DIR,
    THREAD_SETTINGS_FILE,
    TURN_QUEUE_FILE,
    WORK_ITEM_EVENTS_FILE,
    WORK_ITEM_STATES_FILE,
)
from codex_web.providers import (
    get_json as _get_json,
    post_slack_message as _post_slack_message,
    post_telegram_message as _post_telegram_message,
    slack_socket_url as _slack_socket_url,
    update_slack_message as _update_slack_message,
)


BOT_RUNTIME_STATUS: dict[str, dict[str, Any]] = {}
BOT_CHANNEL_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
WATCHDOG_TASK: asyncio.Task[None] | None = None
SUPPORT_SERVICEDESK_SWEEP_TASK: asyncio.Task[None] | None = None
OWNER_WORK_WATCHDOG_TASK: asyncio.Task[None] | None = None
RELEASE_GATE_WATCHDOG_TASK: asyncio.Task[None] | None = None
WORK_ITEM_SLA_TASK: asyncio.Task[None] | None = None
ORCHESTRATOR_WATCHDOG_TASK: asyncio.Task[None] | None = None
SPLIT_BRAIN_WATCHDOG_TASK: asyncio.Task[None] | None = None
QUEUE_RECOVERY_TASK: asyncio.Task[None] | None = None
QUEUE_DRAIN_TASKS: dict[str, asyncio.Task[None]] = {}
WEB_THREAD_RESUME_TASKS: dict[str, asyncio.Task[dict[str, Any]]] = {}
ACTIONABLE_OWNER_CONTINUITY_TASKS: dict[str, asyncio.Task[None]] = {}
HANDOFF_CONTINUITY_TASKS: dict[str, asyncio.Task[None]] = {}
CODEX_TURN_START_LOCK = asyncio.Lock()
TERMINAL_RECOVERY_TASKS: dict[str, asyncio.Task[None]] = {}
THREAD_TERMINAL_FAILURES: dict[str, deque[tuple[float, str]]] = {}
THREAD_LAST_INPUTS: dict[str, dict[str, Any]] = {}
THREAD_REPLACEMENTS: dict[str, str] = {}
THREAD_STEER_TIMES: dict[str, deque[float]] = {}
GITLAB_EVENT_IDS: dict[str, float] = {}
WATCHDOG_DISPATCH_TIMES: dict[str, float] = {}
NATIVE_RECOVERY_LAST_SCHEDULED_AT = 0.0
GITLAB_SYNC_CONSECUTIVE_FAILURES = 0
GITLAB_SYNC_LAST_ERROR: str | None = None
GITLAB_SYNC_LAST_ERROR_AT = 0.0
GITLAB_SYNC_LAST_SUCCESS_AT = 0.0
IS_SHUTTING_DOWN = False
GITLAB_API_BASE = os.environ.get("CODEX_WEB_GITLAB_API_BASE", "https://dev.veridataops.com/gitlab/api/v4")
GITLAB_SEMANTIC_EVENTS_FILE = DATA_DIR / "gitlab_semantic_events.json"
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
DEFAULT_VALIDATION_OWNER = "quinn"
DEFAULT_RELEASE_OWNER = "release manager"
NON_IMPLEMENTATION_OWNERS = {
    DEFAULT_VALIDATION_OWNER,
    DEFAULT_RELEASE_OWNER,
    "orchestrator",
    "carl",
    "compliance manager",
    "nora",
    "maya",
    "larry",
}
HANDOFF_COORDINATION_CHANNEL = "C0B9M89AHCY"


hub = EventHub()




























































def _devhealth_work_item_stats() -> dict[str, int]:
    open_states = [
        state
        for state in _load_work_item_states().values()
        if state.current_stage != "closed"
    ]
    return {
        "open_count": len(open_states),
        "blocked_count": sum(1 for state in open_states if state.current_stage == "failed_with_action_owner"),
        "pending_handoff_count": sum(1 for state in open_states if state.handoff and state.handoff.status == "pending"),
        "release_gate_count": sum(1 for state in open_states if state.release_gate),
        "ready_for_validation_count": sum(1 for state in open_states if state.current_stage == "ready_for_validation"),
        "implementation_active_count": sum(1 for state in open_states if state.current_stage == "implementation_active"),
    }


def _leading_owner_cue_in_action(next_action: str | None) -> str | None:
    action = (next_action or "").strip().lower()
    if not action:
        return None
    owner_candidates = list(OWNER_QUEUE_AGENTS) + ["release manager"]
    for owner in owner_candidates:
        if action.startswith(f"{owner}:"):
            return owner
        if action.startswith(f"{owner} "):
            return owner
    return None




def _maybe_infer_pending_handoff_from_gitlab_projection(
    state: WorkItemState,
    *,
    previous_owner: str | None,
    owners: list[str],
    status_label: str | None,
    now: float,
) -> WorkItemState:
    recipient = _coerce_owner(owners[0]) if owners else None
    if status_label != "status::awaiting confirmation" or not recipient:
        return state
    if state.handoff and state.handoff.status == "pending":
        return state
    sender = _coerce_owner(previous_owner)
    if not sender or sender == recipient:
        sender = _coerce_owner(state.next_owner)
    if not sender or sender == recipient:
        return state
    if state.handoff:
        archive_status = "superseded" if state.handoff.status == "pending" else state.handoff.status
        state = _archive_active_handoff(
            state,
            now=now,
            status=archive_status,
            reason_code="gitlab_projection_inferred",
        )
    state.handoff = WorkItemHandoff(
        from_agent=sender,
        to_agent=recipient,
        reason="Inferred from GitLab awaiting-confirmation label projection.",
        expected_action=state.next_action,
        requested_at=state.last_meaningful_update_at or now,
        status="pending",
        artifact_state=state.artifact_state,
        stage=state.current_stage,
    )
    state.next_owner = recipient
    state = _record_handoff_history(state, state.handoff)
    return state


def _mr_refs_from_payload(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    attrs = payload.get("object_attributes") or {}
    for candidate in (
        attrs.get("source_branch"),
        attrs.get("target_branch"),
        attrs.get("references", {}).get("full") if isinstance(attrs.get("references"), dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            refs.append(candidate.strip())
    changes = payload.get("changes") or {}
    for source in (changes.get("description"), changes.get("title")):
        if isinstance(source, dict):
            for value in source.values():
                if isinstance(value, str):
                    refs.extend(re.findall(r"[A-Za-z0-9._-]+![0-9]+", value))
    return sorted(set(refs))


def _project_issue_ref(payload: dict[str, Any]) -> str | None:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    project_path = str(project.get("path_with_namespace") or "").strip()
    iid = attrs.get("iid")
    if not project_path or iid in {None, ""}:
        return None
    return f"{project_path}#{iid}"


















































WORK_ITEM_WAKEUP_BATCH_HEADER = (
    "Owned-work wakeup batch. Reconcile every listed item against canonical codex-web state, "
    "then process each item that is currently actionable."
)


def _work_item_wakeup_entries(message: str) -> list[dict[str, str]]:
    if message.startswith(WORK_ITEM_WAKEUP_BATCH_HEADER):
        entries: list[dict[str, str]] = []
        for line in message.splitlines()[1:]:
            if not line.startswith("- "):
                continue
            with contextlib.suppress(Exception):
                raw = json.loads(line[2:])
                if isinstance(raw, dict) and raw.get("ref"):
                    entries.append({str(key): str(value) for key, value in raw.items() if value is not None})
        return entries

    ref_match = re.search(r"\bOwned-work wakeup for ([\w.-]+/[\w.-]+#\d+)\b", message, re.IGNORECASE)
    if not ref_match:
        return []

    def field(pattern: str) -> str:
        match = re.search(pattern, message, re.IGNORECASE | re.DOTALL)
        return " ".join((match.group(1) if match else "").split()).strip()

    entry = {
        "ref": ref_match.group(1),
        "classification": field(r"Change classification:\s*([^\.\n]+)"),
        "stage": field(r"Current stage:\s*([^\.\n]+)"),
        "next_action": field(r"Exact next action:\s*(.*?)(?:\s+If blocked,|\Z)"),
    }
    return [{key: value for key, value in entry.items() if value}]


def _render_work_item_wakeup_batch(entries: list[dict[str, str]]) -> str:
    latest_by_ref: dict[str, dict[str, str]] = {}
    for entry in entries:
        ref = entry.get("ref")
        if not ref:
            continue
        normalized = dict(entry)
        if normalized.get("next_action"):
            normalized["next_action"] = _truncate_text(normalized["next_action"], 600)
        latest_by_ref[ref] = normalized
    lines = [WORK_ITEM_WAKEUP_BATCH_HEADER]
    lines.extend(
        "- " + json.dumps(entry, separators=(",", ":"), sort_keys=True)
        for entry in latest_by_ref.values()
    )
    return "\n".join(lines)


def _coalesce_queued_work_item_wakeups(items: list[QueuedTurn]) -> tuple[list[QueuedTurn], bool]:
    candidates = [
        (index, queued, _work_item_wakeup_entries(queued.message))
        for index, queued in enumerate(items)
        if queued.reply_target is None
    ]
    candidates = [candidate for candidate in candidates if candidate[2]]
    if len(candidates) < 2:
        return items, False
    first_index, representative, _ = candidates[0]
    merged_entries = [entry for _, _, entries in candidates for entry in entries]
    representative.message = _render_work_item_wakeup_batch(merged_entries)
    candidate_ids = {id(queued) for _, queued, _ in candidates}
    compacted = [queued for queued in items if id(queued) not in candidate_ids]
    compacted.insert(min(first_index, len(compacted)), representative)
    return compacted, True


def _compact_turn_queues() -> None:
    queues = _load_turn_queues()
    changed = False
    for thread_id, items in list(queues.items()):
        queues[thread_id], queue_changed = _coalesce_queued_work_item_wakeups(items)
        changed = changed or queue_changed
    if changed:
        _save_turn_queues(queues)


def _with_relay_guard(message: str, source: str | None) -> str:
    normalized_source = (source or "").lower()
    if "slack" not in normalized_source:
        return message
    if SLACK_RELAY_NOTICE in message:
        return message
    return f"{SLACK_RELAY_NOTICE}\n\n{message}"


def _turn_source_for_relay_guard(thread_id: str, source: str | None) -> str | None:
    if "slack" in (source or "").lower():
        return source
    if any(binding.provider == "slack" for binding in _bindings_for_thread(thread_id)):
        return f"{source or 'web'}:slack-bound"
    return source


def _turn_failure_text(message: dict[str, Any]) -> str:
    params = message.get("params") or {}
    turn = params.get("turn") or {}
    error = turn.get("error") or params.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        parts = [error.get("message"), error.get("additionalDetails"), error.get("codexErrorInfo")]
        return " ".join(str(part) for part in parts if part)
    return str(error or "")


def _is_unrecoverable_turn_error(error: str) -> bool:
    normalized = error.lower()
    return any(
        marker in normalized
        for marker in (
            "array_above_max_length",
            "array too long",
            "remote compact task",
            "maximum length 16384",
            "context window",
        )
    )


def _append_bot_event(event: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {"created_at": time.time(), **event}
    with BOTS_EVENTS_FILE.open("a") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")






































































def _active_turn_stale_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_ACTIVE_TURN_STALE_SECONDS") or "120")
    except ValueError:
        return 120.0
    return max(30.0, seconds)


def _queue_recovery_interval_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_QUEUE_RECOVERY_SECONDS") or "30")
    except ValueError:
        return 30.0
    if seconds <= 0:
        return 0.0
    return max(10.0, seconds)


def _active_turn_is_stale(thread_id: str | None, max_age: float | None = None) -> bool:
    if not thread_id:
        return False
    max_age = _active_turn_stale_seconds() if max_age is None else max_age
    active = _load_active_turns().get(thread_id)
    return bool(active and time.time() - active.updated_at > max_age)


def _release_stale_active_turn(thread_id: str | None, reason: str) -> None:
    if not thread_id:
        return
    if not _active_turn_is_stale(thread_id):
        return
    _append_bot_event({"type": "stale_active_turn_released", "thread_id": thread_id, "reason": reason})
    _clear_thread_active(thread_id)






async def _dispatch_event_to_binding(binding: BotBinding, text: str, source: str) -> dict[str, Any]:
    project = _project(binding.project_id)
    settings = _thread_run_settings(binding.thread_id)
    effective_model = settings.model or project.model
    effective_reasoning_effort = settings.reasoning_effort
    reply_target = _conversation_target_for_binding(binding)

    async def queue_binding_turn(event_type: str, reason: str | None = None) -> dict[str, Any]:
        duplicate = _find_duplicate_queued_turn(binding.thread_id, message=text, source=source)
        if duplicate:
            _append_bot_event(
                {
                    "type": "event_turn_duplicate_skipped",
                    "source": source,
                    "thread_id": binding.thread_id,
                    "external_conversation_id": binding.external_conversation_id,
                    "queued_id": duplicate.id,
                    "queue_depth": _thread_queue_depth(binding.thread_id),
                }
            )
            await _publish_queue_status(binding.thread_id)
            return {
                "ok": True,
                "queued": True,
                "duplicate": True,
                "threadId": binding.thread_id,
                "queuedId": duplicate.id,
            }
        queued = _enqueue_turn(
            thread_id=binding.thread_id,
            project_id=project.id,
            message=text,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            source=source,
            reply_target=reply_target,
        )
        binding.updated_at = time.time()
        _upsert_bot_binding(binding)
        event_payload = {
            "type": event_type,
            "source": source,
            "thread_id": binding.thread_id,
            "external_conversation_id": binding.external_conversation_id,
            "queued_id": queued.id,
            "queue_depth": _thread_queue_depth(binding.thread_id),
        }
        if reason:
            event_payload["reason"] = _truncate_text(reason, 500)
        _append_bot_event(event_payload)
        await _publish_queue_status(binding.thread_id)
        await hub.publish(
            {
                "type": "bot.inbound",
                "provider": source,
                "externalConversationId": binding.external_conversation_id,
                "threadId": binding.thread_id,
                "queued": True,
                "queuedId": queued.id,
                "queueDepth": _thread_queue_depth(binding.thread_id),
            }
        )
        return {"ok": True, "queued": True, "threadId": binding.thread_id, "queuedId": queued.id}

    _release_stale_active_turn(binding.thread_id, f"{source}:dispatch")
    if _thread_is_active(binding.thread_id) or _thread_queue_depth(binding.thread_id):
        return await queue_binding_turn("event_turn_queued")

    try:
        turn = await _start_thread_turn_now(
            binding.thread_id,
            project=project,
            message=text,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            source=source,
            reply_target=reply_target,
        )
    except Exception as exc:
        if _is_codex_timeout_error(exc):
            return await queue_binding_turn("event_turn_queued_after_timeout", str(getattr(exc, "detail", exc)))
        if not _is_stale_thread_error(exc):
            raise
        binding = await _replace_stale_bot_thread(binding, str(exc))
        project = _project(binding.project_id)
        settings = _thread_run_settings(binding.thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        reply_target = _conversation_target_for_binding(binding)
        turn = await _start_thread_turn_now(
            binding.thread_id,
            project=project,
            message=text,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            source=source,
            reply_target=reply_target,
        )
    binding.updated_at = time.time()
    _upsert_bot_binding(binding)
    await hub.publish(
        {
            "type": "bot.inbound",
            "provider": source,
            "externalConversationId": binding.external_conversation_id,
            "threadId": binding.thread_id,
        }
    )
    return {"ok": True, "queued": False, "threadId": binding.thread_id, "turn": turn}














def _binding_for_external_target(
    provider: str,
    project_id: str,
    external_conversation_id: str,
    external_thread_id: str | None,
) -> BotBinding | None:
    target = _target_for_external_thread(provider, external_conversation_id, external_thread_id)
    if not target:
        return None
    candidates = [
        binding
        for binding in _bindings_for_project(provider, project_id)
        if binding.thread_id == target.thread_id
    ]
    for binding in candidates:
        if binding.external_conversation_id == external_conversation_id:
            return binding
    return candidates[0] if candidates else None


def _clone_binding_for_conversation(source: BotBinding, message: BotInboundMessage) -> BotBinding:
    return _clone_binding_to_conversation(
        source,
        message.external_conversation_id,
        connection_id=message.connection_id or source.connection_id,
        external_name=message.external_name or source.external_name,
        is_primary_channel=False,
    )


def _clone_binding_to_conversation(
    source: BotBinding,
    external_conversation_id: str,
    *,
    connection_id: str | None = None,
    external_name: str | None = None,
    is_primary_channel: bool = False,
) -> BotBinding:
    now = time.time()
    return _upsert_bot_binding(
        BotBinding(
            id=uuid.uuid4().hex[:12],
            connection_id=connection_id or source.connection_id,
            provider=source.provider,
            external_conversation_id=external_conversation_id,
            thread_id=source.thread_id,
            project_id=source.project_id,
            external_name=external_name or external_conversation_id,
            thread_name=source.thread_name,
            route_prefix=source.route_prefix,
            is_master=False,
            is_primary_channel=is_primary_channel,
            post_in_thread=source.post_in_thread,
            sandbox=source.sandbox,
            approval_policy=source.approval_policy,
            created_at=now,
            updated_at=now,
        )
    )


def _cross_channel_binding_for_message(
    provider: str,
    project_id: str,
    current_bindings: list[BotBinding],
    message: BotInboundMessage,
    *,
    allow_bare_prefix: bool = False,
) -> tuple[BotBinding | None, str, bool]:
    current_by_thread_id = {binding.thread_id: binding for binding in current_bindings}
    candidates = [
        binding
        for binding in _bindings_for_project(provider, project_id)
        if binding.external_conversation_id != message.external_conversation_id
    ]
    matches: dict[str, tuple[BotBinding, str]] = {}
    for binding in sorted(candidates, key=lambda item: (len(_binding_prefix(item) or ""), item.updated_at), reverse=True):
        for prefix in _binding_prefix_candidates(binding):
            stripped = _strip_prefix(message.text, prefix, allow_bare_word=allow_bare_prefix)
            if stripped is None:
                continue
            if binding.thread_id in current_by_thread_id:
                return current_by_thread_id[binding.thread_id], stripped, False
            matches.setdefault(binding.thread_id, (binding, stripped))
            break
    if len(matches) == 1:
        return next(iter(matches.values()))[0], next(iter(matches.values()))[1], False
    if len(matches) > 1:
        return None, message.text, True
    primary = _primary_binding_for_project(provider, project_id, message.external_conversation_id)
    if primary:
        return primary, message.text, False
    if len(current_bindings) == 1:
        return current_bindings[0], message.text, False
    return None, message.text, False


def _is_top_level_external_message(message: BotInboundMessage) -> bool:
    return bool(
        message.message_id
        and message.external_thread_id
        and message.external_thread_id == message.message_id
    )


def _has_single_master_binding(bindings: list[BotBinding]) -> bool:
    return len([binding for binding in bindings if binding.is_master]) == 1


def _fallback_binding_for_stale(binding: BotBinding, message: BotInboundMessage) -> BotBinding | None:
    candidates = [
        candidate
        for candidate in _bindings_for_project(binding.provider, binding.project_id)
        if candidate.thread_id != binding.thread_id
    ]
    prefix = _binding_prefix(binding)
    if prefix:
        for candidate in candidates:
            if _binding_prefix(candidate).lower() == prefix.lower():
                return candidate
    masters = [candidate for candidate in candidates if candidate.is_master]
    if len(masters) == 1:
        return masters[0]
    return None


def _upsert_bot_binding(new_binding: BotBinding) -> BotBinding:
    bindings = _load_bot_bindings()
    updated = False
    for index, binding in enumerate(bindings):
        if (
            new_binding.is_master
            and binding.provider == new_binding.provider
            and binding.project_id == new_binding.project_id
            and binding.thread_id != new_binding.thread_id
        ):
            binding.is_master = False
        if (
            binding.provider == new_binding.provider
            and binding.external_conversation_id == new_binding.external_conversation_id
            and binding.thread_id == new_binding.thread_id
        ):
            new_binding.id = binding.id
            new_binding.created_at = binding.created_at
            bindings[index] = new_binding
            updated = True
            break
    if not updated:
        bindings.append(new_binding)
    _save_bot_bindings(bindings)
    _dedupe_bot_integrations()
    return new_binding


def _remove_bot_binding(binding_id: str) -> None:
    bindings = [binding for binding in _load_bot_bindings() if binding.id != binding_id]
    _save_bot_bindings(bindings)


def _channel_label(channel_id: str, name: str | None = None) -> str:
    normalized = (name or "").strip()
    if not normalized:
        return channel_id
    return normalized if normalized.startswith("#") else f"#{normalized}"


def _known_bot_channels(project_id: str) -> list[dict[str, str]]:
    channels: dict[tuple[str, str], dict[str, str]] = {}
    for connection in _load_bot_connections():
        if connection.project_id != project_id or not connection.default_external_conversation_id:
            continue
        key = (connection.provider, connection.default_external_conversation_id)
        channels[key] = {
            "provider": connection.provider,
            "id": connection.default_external_conversation_id,
            "name": connection.default_external_name or "",
            "label": _channel_label(
                connection.default_external_conversation_id,
                connection.default_external_name,
            ),
        }
    for binding in _load_bot_bindings():
        if binding.project_id != project_id:
            continue
        key = (binding.provider, binding.external_conversation_id)
        channels.setdefault(
            key,
            {
                "provider": binding.provider,
                "id": binding.external_conversation_id,
                "name": binding.external_name or "",
                "label": _channel_label(binding.external_conversation_id, binding.external_name),
            },
        )
    return sorted(channels.values(), key=lambda item: (item["provider"], item["label"]))


def _slack_channel_for_connection(connection: BotConnection, channel_id: str) -> dict[str, str] | None:
    if not connection.bot_token:
        return None
    response = _get_json(
        f"https://slack.com/api/conversations.info?channel={channel_id}",
        {"Authorization": f"Bearer {connection.bot_token}"},
    )
    if not response.get("ok"):
        return None
    channel = response.get("channel") or {}
    name = channel.get("name") or channel.get("name_normalized")
    if not name:
        return None
    return {
        "provider": "slack",
        "id": channel_id,
        "name": name,
        "label": _channel_label(channel_id, name),
    }


def _channel_needs_name(channel: dict[str, str]) -> bool:
    channel_id = channel.get("id") or ""
    name = channel.get("name") or ""
    label = channel.get("label") or ""
    return not name or name == channel_id or label in {channel_id, f"#{channel_id}"}


def _slack_channels_for_connection(connection: BotConnection) -> list[dict[str, str]]:
    if not connection.bot_token:
        return []
    channels: list[dict[str, str]] = []
    cursor = ""
    for _ in range(20):
        url = "https://slack.com/api/conversations.list?exclude_archived=true&limit=200&types=public_channel,private_channel"
        if cursor:
            url += f"&cursor={cursor}"
        response = _get_json(url, {"Authorization": f"Bearer {connection.bot_token}"})
        if not response.get("ok"):
            break
        for channel in response.get("channels") or []:
            channel_id = channel.get("id")
            if not channel_id:
                continue
            name = channel.get("name") or channel.get("name_normalized") or channel_id
            channels.append(
                {
                    "provider": "slack",
                    "id": channel_id,
                    "name": name,
                    "label": _channel_label(channel_id, name),
                }
            )
        cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break
    return channels


def _bot_channels(project_id: str) -> list[dict[str, str]]:
    cached = BOT_CHANNEL_CACHE.get(project_id)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    channels = {(item["provider"], item["id"]): item for item in _known_bot_channels(project_id)}
    for connection in _load_bot_connections():
        if connection.project_id != project_id or connection.provider != "slack":
            continue
        with contextlib.suppress(Exception):
            for channel in _slack_channels_for_connection(connection):
                channels[(channel["provider"], channel["id"])] = channel
        for channel in list(channels.values()):
            if channel["provider"] != "slack" or not _channel_needs_name(channel):
                continue
            with contextlib.suppress(Exception):
                resolved = _slack_channel_for_connection(connection, channel["id"])
                if resolved:
                    channels[(resolved["provider"], resolved["id"])] = resolved
    result = sorted(channels.values(), key=lambda item: (item["provider"], item["label"]))
    BOT_CHANNEL_CACHE[project_id] = (time.time(), result)
    return result


async def _set_thread_primary(thread_id: str, project_id: str, primary: bool) -> list[BotBinding]:
    project = _project(project_id)
    bindings = _load_bot_bindings()
    if primary and not any(binding.thread_id == thread_id and binding.project_id == project.id for binding in bindings):
        await _project_scoped_bindings_for_thread(thread_id)
        bindings = _load_bot_bindings()
    changed = False
    now = time.time()
    for binding in bindings:
        if binding.project_id != project.id:
            continue
        next_master = bool(primary and binding.thread_id == thread_id)
        if binding.is_master != next_master:
            binding.is_master = next_master
            binding.updated_at = now
            changed = True
    if changed:
        _save_bot_bindings(bindings)
    return [binding for binding in bindings if binding.project_id == project.id]


async def _set_thread_primary_channel(
    thread_id: str,
    project_id: str,
    provider: str,
    external_conversation_id: str | None,
) -> list[BotBinding]:
    project = _project(project_id)
    normalized_provider = provider.lower()
    if normalized_provider not in {"slack", "telegram"}:
        raise HTTPException(status_code=400, detail="Provider must be slack or telegram")
    bindings = _load_bot_bindings()
    if external_conversation_id and not any(
        binding.thread_id == thread_id
        and binding.project_id == project.id
        and binding.provider == normalized_provider
        and binding.external_conversation_id == external_conversation_id
        for binding in bindings
    ):
        candidates = [
            binding
            for binding in bindings
            if binding.thread_id == thread_id and binding.project_id == project.id and binding.provider == normalized_provider
        ]
        if not candidates:
            candidates = await _project_scoped_bindings_for_thread(thread_id)
        source = next((binding for binding in candidates if binding.provider == normalized_provider), None)
        if not source:
            connection = next(
                (
                    connection
                    for connection in _load_bot_connections()
                    if connection.project_id == project.id and connection.provider == normalized_provider
                ),
                None,
            )
            if not connection:
                raise HTTPException(status_code=400, detail="No bot connection is configured for this project")
            source = await _start_bot_thread(
                BotBindingCreate(
                    connection_id=connection.id,
                    provider=normalized_provider,
                    external_conversation_id=external_conversation_id,
                    project_id=project.id,
                    thread_id=thread_id,
                )
            )
        else:
            source = _upsert_bot_binding(
                BotBinding(
                    **{
                        **source.model_dump(),
                        "id": uuid.uuid4().hex[:12],
                        "external_conversation_id": external_conversation_id,
                        "external_name": external_conversation_id,
                        "is_master": False,
                        "is_primary_channel": False,
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    }
                )
            )
        bindings = _load_bot_bindings()
    changed = False
    now = time.time()
    for binding in bindings:
        if binding.thread_id != thread_id or binding.project_id != project.id or binding.provider != normalized_provider:
            continue
        next_primary = bool(external_conversation_id and binding.external_conversation_id == external_conversation_id)
        if binding.is_primary_channel != next_primary:
            binding.is_primary_channel = next_primary
            binding.updated_at = now
            changed = True
    if changed:
        _save_bot_bindings(bindings)
    return [binding for binding in bindings if binding.thread_id == thread_id and binding.project_id == project.id]




def _project(project_id: str | None) -> Project:
    projects = _load_projects()
    if project_id is None:
        return projects[0]
    for project in projects:
        if project.id == project_id:
            return project
    raise HTTPException(status_code=404, detail="Project not found")


def _project_params(project: Project, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": project.path,
        "sandbox": project.sandbox,
        "approvalPolicy": project.approval_policy,
        "approvalsReviewer": "user",
    }
    if project.model:
        params["model"] = project.model
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                params[key] = value
    return params


async def _start_bot_thread(binding_create: BotBindingCreate) -> BotBinding:
    connection = _bot_connection(binding_create.connection_id) if binding_create.connection_id else None
    provider = (binding_create.provider or (connection.provider if connection else "")).lower()
    if provider not in {"slack", "telegram"}:
        raise HTTPException(status_code=400, detail="Provider must be slack or telegram")
    project_id = binding_create.project_id or connection.project_id if connection else binding_create.project_id
    external_conversation_id = (
        binding_create.external_conversation_id
        or (connection.default_external_conversation_id if connection else None)
    )
    if not external_conversation_id:
        raise HTTPException(status_code=400, detail="External conversation id is required")
    project = _project(project_id)
    now = time.time()
    if binding_create.thread_id:
        thread_id = binding_create.thread_id
    else:
        response = await codex.request(
            "thread/start",
            _project_params(
                project,
                {
                    "sandbox": binding_create.sandbox,
                    "approvalPolicy": binding_create.approval_policy,
                    "sessionStartSource": "startup",
                },
            ),
        )
        thread_id = response["thread"]["id"]
    thread_name = binding_create.thread_name or binding_create.route_prefix
    if thread_name:
        await _set_thread_name(thread_id, thread_name)
    return _upsert_bot_binding(
        BotBinding(
            id=uuid.uuid4().hex[:12],
            connection_id=connection.id if connection else binding_create.connection_id,
            provider=provider,
            external_conversation_id=external_conversation_id,
            external_name=binding_create.external_name or (connection.default_external_name if connection else None),
            thread_name=thread_name,
            route_prefix=binding_create.route_prefix or thread_name,
            is_master=binding_create.is_master,
            is_primary_channel=binding_create.is_primary_channel,
            post_in_thread=binding_create.post_in_thread,
            thread_id=thread_id,
            project_id=project_id,
            sandbox=binding_create.sandbox,
            approval_policy=binding_create.approval_policy,
            created_at=now,
            updated_at=now,
        )
    )


def _is_stale_thread_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "no rollout found for thread id",
            "thread not found",
            "already has an active writer",
            "thread-store conflict",
        )
    )


def _is_codex_timeout_error(exc: Exception) -> bool:
    detail = getattr(exc, "detail", None)
    text = str(detail or exc).lower()
    return getattr(exc, "status_code", None) == 504 or "timed out after" in text


def _thread_read_timeout_response(
    thread_id: str,
    limit: int,
    exc: Exception | str,
    *,
    event_type: str = "web_read_timeout",
) -> dict[str, Any]:
    indexed = next((thread for thread in _load_thread_index() if thread.id == thread_id), None)
    bindings = _bindings_for_thread(thread_id)
    binding = bindings[0] if bindings else None
    thread = {
        "id": thread_id,
        "name": (indexed.name if indexed else None) or (binding.thread_name if binding else None) or "Untitled thread",
        "cwd": (indexed.cwd if indexed else None),
        "path": (indexed.path if indexed else None),
        "turns": [],
        "status": {"type": "notLoaded"},
        "messageLimit": limit,
        "readTimedOut": True,
    }
    error = exc if isinstance(exc, str) else str(getattr(exc, "detail", exc))
    _append_bot_event(
        {
            "type": event_type,
            "thread_id": thread_id,
            "error": _truncate_text(error, 500),
        }
    )
    return {
        "ok": False,
        "timedOut": True,
        "threadId": thread_id,
        "error": error,
        "thread": thread,
    }


def _web_thread_resume_handoff_timeout() -> float:
    try:
        return max(0.1, float(os.environ.get("CODEX_WEB_RESUME_HANDOFF_TIMEOUT") or "3"))
    except ValueError:
        return 3


def _thread_resume_retry_delay() -> float:
    try:
        return max(1.0, float(os.environ.get("CODEX_WEB_RESUME_RETRY_DELAY") or "30"))
    except ValueError:
        return 30


async def _run_web_thread_resume(
    thread_id: str,
    project_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        return await codex.request("thread/resume", params)
    except Exception as exc:
        payload = {
            "thread_id": thread_id,
            "project_id": project_id,
            "error": _truncate_text(str(getattr(exc, "detail", exc)), 500),
        }
        if _is_codex_timeout_error(exc):
            _append_bot_event({"type": "web_resume_timeout", **payload})
            return {
                "ok": False,
                "timedOut": True,
                "threadId": thread_id,
                "error": str(getattr(exc, "detail", exc)),
            }
        _append_bot_event({"type": "web_resume_failed", **payload})
        return {
            "ok": False,
            "threadId": thread_id,
            "error": str(getattr(exc, "detail", exc)),
        }
    finally:
        current = asyncio.current_task()
        if WEB_THREAD_RESUME_TASKS.get(thread_id) is current:
            WEB_THREAD_RESUME_TASKS.pop(thread_id, None)


def _web_thread_resume_task(
    thread_id: str,
    project_id: str,
    params: dict[str, Any],
) -> tuple[asyncio.Task[dict[str, Any]], bool]:
    existing = WEB_THREAD_RESUME_TASKS.get(thread_id)
    if existing and not existing.done():
        return existing, False
    task = asyncio.create_task(_run_web_thread_resume(thread_id, project_id, params))
    WEB_THREAD_RESUME_TASKS[thread_id] = task
    return task, True


def _is_transient_websocket_disconnect(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, ConnectionClosed) or "keepalive ping timeout" in text or "no close frame received" in text


def _steer_route_message(message: BotInboundMessage) -> tuple[bool, BotInboundMessage]:
    match = re.match(r"^\s*steer(?:\s+|:\s*)(.+)$", message.text, re.IGNORECASE | re.DOTALL)
    if not match:
        return False, message
    routed_text = match.group(1).strip()
    if not routed_text:
        return False, message
    return True, message.model_copy(update={"text": routed_text})


def _resolve_bot_binding(
    bindings: list[BotBinding],
    message: BotInboundMessage,
    *,
    prefer_external_thread: bool = True,
    allow_master_fallback: bool = True,
    allow_bare_prefix: bool = False,
) -> tuple[BotBinding | None, str, bool]:
    text = message.text
    if not bindings:
        return None, text, False
    if prefer_external_thread:
        thread_binding = _binding_for_external_thread(bindings, message.external_thread_id)
        if thread_binding:
            return thread_binding, text, False
    for binding in sorted(bindings, key=lambda item: (len(_binding_prefix(item) or ""), item.updated_at), reverse=True):
        for prefix in _binding_prefix_candidates(binding):
            stripped = _strip_prefix(text, prefix, allow_bare_word=allow_bare_prefix)
            if stripped is not None:
                return binding, stripped, False
    if allow_master_fallback:
        masters = [binding for binding in bindings if binding.is_master]
        if len(masters) == 1:
            return masters[0], text, False
    return None, text, allow_master_fallback


def _binding_for_external_thread(bindings: list[BotBinding], external_thread_id: str | None) -> BotBinding | None:
    if not external_thread_id:
        return None
    by_thread_id = {binding.thread_id: binding for binding in bindings}
    for targets in (_load_bot_reply_targets(), _load_bot_delivery_targets()):
        for target in targets.values():
            if not any(
                target.provider == binding.provider and target.external_conversation_id == binding.external_conversation_id
                for binding in bindings
            ):
                continue
            if target.external_thread_id != external_thread_id and target.message_id != external_thread_id:
                continue
            binding = by_thread_id.get(target.thread_id)
            if binding:
                return binding
    return None


def _binding_prefix(binding: BotBinding) -> str:
    return (binding.route_prefix or binding.thread_name or binding.thread_id).strip()


def _binding_report_name(binding: BotBinding) -> str:
    prefix = _binding_prefix(binding)
    if " - " in prefix:
        first, rest = prefix.split(" - ", 1)
        if first.strip() and "agent" in rest.lower():
            return first.strip()
    return prefix


def _binding_prefix_candidates(binding: BotBinding) -> list[str]:
    candidates = [
        _binding_prefix(binding),
        _binding_report_name(binding),
        binding.thread_name or "",
    ]
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _strip_prefix(text: str, prefix: str, *, allow_bare_word: bool = False) -> str | None:
    normalized = text.strip()
    prefix = prefix.strip()
    candidates = [
        f"{prefix}:",
        f"{prefix} -",
        f"[{prefix}]",
        f"@{prefix}",
    ]
    lower = normalized.lower()
    for candidate in candidates:
        if lower.startswith(candidate.lower()):
            return normalized[len(candidate) :].strip()
    if allow_bare_word:
        match = re.match(rf"^{re.escape(prefix)}(?:\s+)(.+)$", normalized, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return None


def _strip_slack_mentions(text: str) -> str:
    return re.sub(r"^(?:<@[A-Z0-9]+>\s*)+", "", text.strip()).strip()


def _ambiguous_route_message(prefixes: list[str]) -> str:
    unique = sorted({prefix for prefix in prefixes if prefix})
    if not unique:
        return "I found multiple Codex thread bindings for this conversation, but none have a usable prefix."
    return "Please include a thread prefix: " + ", ".join(f"`{prefix}:`" for prefix in unique)


def _format_bot_prompt(message: BotInboundMessage, provider: str, text: str) -> str:
    sender = message.sender_name or message.sender_id or "unknown sender"
    return (
        f"Message received from {provider} conversation "
        f"{message.external_conversation_id} by {sender}.\n\n"
        f"{text}"
    )


async def _set_thread_name(thread_id: str, name: str) -> dict[str, Any]:
    response = await codex.request("thread/name/set", {"threadId": thread_id, "name": name})
    indexed = IndexedThread(id=thread_id, name=name, updatedAt=time.time())
    with contextlib.suppress(Exception):
        thread_response = await codex.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
        indexed = IndexedThread(
            id=thread_id,
            name=name,
            cwd=thread.get("cwd"),
            path=thread.get("path"),
            updatedAt=thread.get("updatedAt") or time.time(),
        )
    _upsert_indexed_thread(indexed)
    return response


def _canonical_bot_thread_names() -> dict[str, str]:
    grouped: dict[str, list[BotBinding]] = {}
    for binding in _load_bot_bindings():
        if binding.thread_name:
            grouped.setdefault(binding.thread_id, []).append(binding)
    names: dict[str, str] = {}
    for thread_id, bindings in grouped.items():
        bindings.sort(key=lambda binding: (binding.created_at, binding.updated_at))
        for binding in bindings:
            name = (binding.thread_name or "").strip()
            if name:
                names[thread_id] = name
                break
    return names


async def _restore_bot_thread_name(thread_id: str | None) -> None:
    if not thread_id:
        return
    name = _canonical_bot_thread_names().get(thread_id)
    if not name:
        return
    try:
        thread_response = await codex.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
        if (thread.get("name") or "").strip() == name:
            return
        await _set_thread_name(thread_id, name)
        _append_bot_event({"type": "bot_thread_name_restored", "thread_id": thread_id, "name": name})
    except Exception as exc:
        _append_bot_event({"type": "bot_thread_name_restore_failed", "thread_id": thread_id, "error": str(exc)})


async def _restore_bot_thread_names() -> None:
    for thread_id in _canonical_bot_thread_names():
        await _restore_bot_thread_name(thread_id)


def _project_for_cwd(cwd: str | None) -> Project | None:
    if not cwd:
        return None
    for project in _load_projects():
        if project.path == cwd:
            return project
    return None


async def _project_scoped_bindings_for_thread(thread_id: str) -> list[BotBinding]:
    try:
        thread_response = await codex.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    except Exception:
        return []
    thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
    project = _project_for_cwd(thread.get("cwd"))
    if not project:
        return []
    thread_name = thread.get("name") or thread.get("agentNickname") or thread_id
    now = time.time()
    bindings: list[BotBinding] = []
    for connection in _load_bot_connections():
        if connection.project_id != project.id or not connection.default_external_conversation_id:
            continue
        binding = _upsert_bot_binding(
            BotBinding(
                id=uuid.uuid4().hex[:12],
                connection_id=connection.id,
                provider=connection.provider,
                external_conversation_id=connection.default_external_conversation_id,
                thread_id=thread_id,
                project_id=project.id,
                external_name=connection.default_external_name,
                thread_name=thread_name,
                route_prefix=thread_name,
                is_primary_channel=False,
                post_in_thread=False,
                sandbox=project.sandbox,
                approval_policy=project.approval_policy,
                created_at=now,
                updated_at=now,
            )
        )
        bindings.append(binding)
    return bindings


def _mentioned_work_item_states(text: str) -> list[WorkItemState]:
    states = _load_work_item_states()
    refs = set(re.findall(r"\b[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+\b", text))
    mentioned = [states[ref] for ref in refs if ref in states]
    for iid in set(re.findall(r"(?<![A-Za-z0-9_./-])#(\d+)\b", text)):
        matches = [state for ref, state in states.items() if ref.endswith(f"#{iid}")]
        if len(matches) == 1 and all(state.ref != matches[0].ref for state in mentioned):
            mentioned.append(matches[0])
    return mentioned


def _workflow_outbound_claim_findings(text: str) -> tuple[list[str], list[WorkItemState]]:
    mentioned = _mentioned_work_item_states(text)
    if len(mentioned) != 1:
        return [], mentioned
    state = _ensure_work_item_lane_defaults(mentioned[0])
    findings: list[str] = []
    owner_names = sorted(
        set(OWNER_QUEUE_AGENTS) | {"orchestrator", "release manager", "compliance manager"},
        key=len,
        reverse=True,
    )
    owners = "|".join(re.escape(owner) for owner in owner_names)
    owner_patterns = (
        rf"\bcurrent[_ ]owner\s*[=:]\s*`?({owners})\b",
        rf"\bowned by\s+`?({owners})\b",
        rf"\bownership\s+(?:moved|transferred)\s+to\s+`?({owners})\b",
    )
    claimed_owner: str | None = None
    for pattern in owner_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            claimed_owner = _coerce_owner(match.group(1))
            break
    canonical_owner = _coerce_owner(state.current_owner)
    if claimed_owner and claimed_owner != canonical_owner:
        findings.append(
            f"owner claim mismatch for {state.ref}: claimed={claimed_owner} canonical={canonical_owner or 'none'}"
        )

    stage_match = re.search(r"\bcurrent[_ ]stage\s*[=:]\s*`?([a-z][a-z0-9 _-]*)", text, re.IGNORECASE)
    if stage_match:
        claimed_stage = stage_match.group(1).strip().lower().replace(" ", "_").rstrip("._-")
        if claimed_stage != state.current_stage:
            findings.append(
                f"stage claim mismatch for {state.ref}: claimed={claimed_stage} canonical={state.current_stage}"
            )

    if re.search(r"\bhandoff\s+(?:is\s+|was\s+)?accepted\b", text, re.IGNORECASE):
        handoff_status = state.handoff.status if state.handoff else None
        if handoff_status != "accepted":
            findings.append(
                f"handoff claim mismatch for {state.ref}: claimed=accepted canonical={handoff_status or 'none'}"
            )
    return findings, mentioned


def _canonical_workflow_correction(report_name: str, states: list[WorkItemState], findings: list[str]) -> str:
    snapshots = []
    for state in states:
        handoff = state.handoff.status if state.handoff else "none"
        snapshots.append(
            f"{state.ref}: current_owner={state.current_owner or 'none'}, "
            f"current_stage={state.current_stage}, handoff={handoff}, "
            f"next_owner={state.next_owner or 'none'}"
        )
    return (
        f"{report_name}: Codex-web withheld an agent update because its workflow claim conflicted "
        f"with canonical state. {'; '.join(findings)}. Canonical state: {'; '.join(snapshots)}."
    )


def _is_details_command(text: str) -> bool:
    return text.strip().lower() in {"details", "detail"}


def _format_bot_detail_response(detail: BotThreadDetail) -> str:
    return f"{detail.title}\n{_format_code_block(_truncate_text(detail.text))}"


def _format_bot_detail_item(item: dict[str, Any]) -> dict[str, str] | None:
    item_type = item.get("type")
    if item_type == "commandExecution":
        command = (item.get("command") or "").strip()
        output = (item.get("aggregatedOutput") or "").strip()
        if not command and not output:
            return None
        body = []
        if command:
            body.append(f"$ {command}")
        body.append(output or "No command output.")
        return {"title": "Command details", "text": "\n\n".join(body)}

    if item_type == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        if not changes:
            return None
        parts = []
        for change in changes:
            path = change.get("path") or "unknown path"
            kind = change.get("kind") or "change"
            diff = (change.get("diff") or "").strip()
            parts.append(f"# {kind}: {path}\n{diff}".strip())
        summary = "File details" if len(changes) == 1 else f"File details ({len(changes)} files)"
        return {"title": summary, "text": "\n\n".join(parts)}

    return None


def _truncate_text(text: str, limit: int = 28000) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n\n... truncated {omitted} chars"


def _format_code_block(text: str, language: str = "") -> str:
    sanitized = text.replace("```", "'''")
    return f"```{language}\n{sanitized}\n```"


def _strip_outbound_sender_prefix(text: str, prefix: str | None) -> str:
    normalized = (text or "").strip()
    sender = (prefix or "").strip()
    if not normalized or not sender:
        return normalized
    stripped = _strip_prefix(normalized, sender)
    return stripped if stripped else normalized


def _format_bot_outbound_item(item: dict[str, Any], prefix: str | None) -> str | None:
    item_type = item.get("type")
    if item_type == "agentMessage":
        text = _strip_outbound_sender_prefix(item.get("text") or "", prefix)
        return text or None

    if item_type == "commandExecution":
        command = (item.get("command") or "").strip()
        output = (item.get("aggregatedOutput") or "").strip()
        if not command and not output:
            return None
        parts = ["Command result"]
        if command:
            parts.append(_format_code_block(_truncate_text(command, 3000), "sh"))
        if output:
            parts.append(_format_code_block(_truncate_text(output)))
        else:
            parts.append("_No command output._")
        return "\n".join(parts)

    if item_type == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        if not changes:
            return None
        summary = f"{len(changes)} file changed" if len(changes) == 1 else f"{len(changes)} files changed"
        parts = [summary]
        remaining = 26000
        for change in changes[:5]:
            path = change.get("path") or "unknown path"
            kind = change.get("kind") or "change"
            diff = (change.get("diff") or "").strip()
            header = f"*{_slack_escape(kind)}*: `{_slack_escape(path)}`"
            if diff:
                snippet = _truncate_text(diff, max(1000, remaining))
                remaining -= len(snippet)
                parts.append(f"{header}\n{_format_code_block(snippet, 'diff')}")
            else:
                parts.append(header)
            if remaining <= 0:
                break
        if len(changes) > 5:
            parts.append(f"... plus {len(changes) - 5} more file changes")
        return "\n".join(parts)

    return None


def _active_turn_source(thread_id: str | None) -> str:
    if not thread_id:
        return ""
    active = _load_active_turns().get(thread_id)
    return (active.source or "") if active else ""


def _has_recent_reply_target_for_active_turn(binding: BotBinding) -> bool:
    active = _load_active_turns().get(binding.thread_id)
    if not active:
        return False
    target = _active_reply_target_for_binding(binding) or _reply_target_for_binding(binding)
    if not target:
        return False
    return target.updated_at >= active.started_at - 30


def _should_reply_in_external_thread(binding: BotBinding) -> bool:
    source = _active_turn_source(binding.thread_id).lower()
    return binding.post_in_thread or "slack" in source or _has_recent_reply_target_for_active_turn(binding)


def _slack_reply_username(binding: BotBinding) -> str:
    name = _binding_report_name(binding)
    return f"Codex · {name}" if name else "Codex"


def _slack_reply_icon(binding: BotBinding) -> str:
    icons = [
        ":large_blue_circle:",
        ":large_green_circle:",
        ":large_orange_circle:",
        ":large_purple_circle:",
        ":large_yellow_circle:",
        ":red_circle:",
        ":black_circle:",
        ":white_circle:",
        ":brown_circle:",
        ":large_red_square:",
        ":large_blue_square:",
        ":large_green_square:",
        ":large_yellow_square:",
        ":large_orange_square:",
        ":large_purple_square:",
        ":large_brown_square:",
        ":black_large_square:",
        ":white_large_square:",
        ":small_blue_diamond:",
        ":small_orange_diamond:",
        ":large_blue_diamond:",
        ":large_orange_diamond:",
        ":small_red_triangle:",
        ":small_red_triangle_down:",
        ":eight_pointed_black_star:",
        ":six_pointed_star:",
        ":star:",
        ":sparkles:",
        ":zap:",
        ":fire:",
        ":snowflake:",
        ":sunny:",
        ":crescent_moon:",
        ":cloud:",
        ":umbrella:",
        ":coffee:",
        ":rocket:",
        ":satellite:",
        ":gear:",
        ":mag:",
        ":lock:",
        ":key:",
        ":bell:",
        ":bookmark:",
        ":pushpin:",
        ":paperclip:",
        ":scissors:",
        ":hammer:",
        ":wrench:",
        ":pick:",
        ":shield:",
        ":link:",
        ":package:",
        ":battery:",
        ":bulb:",
        ":hourglass:",
        ":watch:",
        ":compass:",
        ":anchor:",
    ]
    thread_id = binding.thread_id
    assignments = _load_slack_thread_icons()
    assigned = assignments.get(thread_id)
    if assigned:
        return assigned
    used = set(assignments.values())
    seed = f"{_binding_prefix(binding)}:{thread_id}"
    start = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % len(icons)
    for offset in range(len(icons)):
        candidate = icons[(start + offset) % len(icons)]
        if candidate not in used:
            assignments[thread_id] = candidate
            _save_slack_thread_icons(assignments)
            return candidate
    candidate = icons[start]
    assignments[thread_id] = candidate
    _save_slack_thread_icons(assignments)
    return candidate


def _request_id_value(request_id: int | str) -> int | str:
    return int(request_id) if isinstance(request_id, str) and request_id.isdigit() else request_id


def _nested_value(payload: Any, keys: set[str]) -> Any:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and value:
                return value
        for value in payload.values():
            found = _nested_value(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_value(value, keys)
            if found:
                return found
    return None


def _thread_id_for_turn_id(turn_id: str | None) -> str | None:
    if not turn_id:
        return None
    for thread_id, active in _load_active_turns().items():
        if active.turn_id == turn_id:
            return thread_id
    return None


def _approval_thread_id(request: dict[str, Any]) -> str | None:
    params = request.get("params") or {}
    thread_id = (
        params.get("threadId")
        or params.get("thread_id")
        or params.get("conversationId")
        or params.get("conversation_id")
        or (params.get("item") or {}).get("threadId")
        or _nested_value(params, {"threadId", "thread_id", "conversationId", "conversation_id"})
    )
    if thread_id:
        return str(thread_id)
    turn_id = (
        params.get("turnId")
        or params.get("turn_id")
        or (params.get("turn") or {}).get("id")
        or (params.get("item") or {}).get("turnId")
        or _nested_value(params, {"turnId", "turn_id"})
    )
    return _thread_id_for_turn_id(str(turn_id) if turn_id else None)


def _approval_run_settings(request: dict[str, Any]) -> ThreadRunSettings:
    thread_id = _approval_thread_id(request)
    settings = _thread_run_settings(thread_id)
    if settings.approval_policy:
        return settings
    active = _load_active_turns().get(thread_id or "")
    if active and (active.sandbox or active.approval_policy):
        return ThreadRunSettings(sandbox=active.sandbox, approval_policy=active.approval_policy)
    return settings


def _slack_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _approval_summary(request: dict[str, Any]) -> str:
    params = request.get("params") or {}
    method = request.get("method") or "approval"
    command = (
        params.get("command")
        or params.get("reason")
        or (params.get("item") or {}).get("command")
        or (params.get("action") or {}).get("command")
    )
    if command:
        return str(command)
    action = params.get("action") or {}
    if action.get("type") == "applyPatch":
        return "Apply patch: " + ", ".join(action.get("files") or [])
    if method == "item/permissions/requestApproval":
        return params.get("reason") or "Permission change requested"
    text = json.dumps(params, separators=(",", ":"))
    return text[:700] + ("..." if len(text) > 700 else "")


def _approval_blocks(request: dict[str, Any], binding: BotBinding) -> list[dict[str, Any]]:
    request_id = request.get("id")
    prefix = _binding_prefix(binding)
    summary = _slack_escape(_approval_summary(request))
    context = _slack_escape(prefix or binding.thread_id)

    def value(decision: str) -> str:
        return json.dumps({"request_id": request_id, "decision": decision}, separators=(",", ":"))

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Approval requested for `{context}`*\n```{summary}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve once"},
                    "style": "primary",
                    "action_id": "codex_approval_accept",
                    "value": value("accept"),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve session"},
                    "action_id": "codex_approval_accept_session",
                    "value": value("acceptForSession"),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "codex_approval_decline",
                    "value": value("decline"),
                },
            ],
        },
    ]


def _approval_resolved_blocks(request: dict[str, Any] | None, context: str, status: str) -> list[dict[str, Any]]:
    summary = _slack_escape(_approval_summary(request)) if request else "This approval request is no longer pending."
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Approval resolved for `{_slack_escape(context)}`*\n{_slack_escape(status)}\n```{summary}```",
            },
        }
    ]


def _slack_interaction_message_ts(payload: dict[str, Any]) -> str | None:
    container = payload.get("container") or {}
    message = payload.get("message") or {}
    return container.get("message_ts") or message.get("ts")


def _slack_interaction_context(payload: dict[str, Any], fallback: str) -> str:
    blocks = (payload.get("message") or {}).get("blocks") or []
    for block in blocks:
        text = (block.get("text") or {}).get("text") if isinstance(block, dict) else None
        if not text:
            continue
        match = re.search(r"Approval requested for `([^`]+)`", text)
        if match:
            return match.group(1)
    return fallback


def _set_runtime_status(connection: BotConnection, status: str, **details: Any) -> None:
    current = BOT_RUNTIME_STATUS.get(connection.id, {})
    current.update(
        {
            "connectionId": connection.id,
            "provider": connection.provider,
            "name": connection.name,
            "status": status,
            "updatedAt": time.time(),
            **details,
        }
    )
    BOT_RUNTIME_STATUS[connection.id] = current


def _recent_inbound_message_ids(limit: int = 2000) -> set[str]:
    if not BOTS_EVENTS_FILE.exists():
        return set()
    with BOTS_EVENTS_FILE.open(errors="replace") as handle:
        lines = deque(handle, maxlen=max(1, limit))
    message_ids: set[str] = set()
    for line in lines:
        with contextlib.suppress(Exception):
            event = json.loads(line)
            if event.get("provider") == "slack" and event.get("message_id"):
                message_ids.add(str(event["message_id"]))
    return message_ids


def _verify_slack_signature(request: Request, body: bytes) -> None:
    signing_secrets = [os.environ.get("SLACK_SIGNING_SECRET")]
    signing_secrets.extend(connection.signing_secret for connection in _load_bot_connections() if connection.provider == "slack")
    signing_secrets = [secret for secret in signing_secrets if secret]
    if not signing_secrets:
        return
    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp") from exc
    if abs(time.time() - request_time) > 300:
        raise HTTPException(status_code=401, detail="Stale Slack signature")
    basestring = f"v0:{timestamp}:{body.decode()}".encode()
    for signing_secret in signing_secrets:
        expected = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return
    raise HTTPException(status_code=401, detail="Invalid Slack signature")


def _verify_telegram_secret(request: Request) -> None:
    expected_secrets = [os.environ.get("TELEGRAM_WEBHOOK_SECRET")]
    expected_secrets.extend(connection.webhook_secret for connection in _load_bot_connections() if connection.provider == "telegram")
    expected_secrets = [secret for secret in expected_secrets if secret]
    if not expected_secrets:
        return
    received = request.headers.get("x-telegram-bot-api-secret-token")
    if not received:
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")
    for expected in expected_secrets:
        if hmac.compare_digest(expected, received):
            return
    raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")






codex: Any = None
bot_runtime: Any = None
app = FastAPI(title="Codex Web Local")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _sd_notify(message: str) -> bool:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return False
    address: str | bytes = notify_socket
    if notify_socket.startswith("@"):
        address = "\0" + notify_socket[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.connect(address)
            client.sendall(message.encode())
        return True
    except OSError:
        return False


def _watchdog_interval() -> float:
    try:
        usec = int(os.environ.get("WATCHDOG_USEC") or "0")
    except ValueError:
        return 0
    if usec <= 0:
        return 0
    return max(5.0, min(30.0, usec / 2_000_000))


def _autonomy_enabled() -> bool:
    if (DATA_DIR / "AUTONOMY_DISABLED").exists():
        return False
    value = (os.environ.get("CODEX_WEB_AUTONOMY_ENABLED") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _owner_work_watchdog_interval() -> float:
    if not _autonomy_enabled():
        return 0
    try:
        seconds = float(os.environ.get("CODEX_WEB_OWNER_WORK_WATCHDOG_SECONDS") or "600")
    except ValueError:
        return 600.0
    if seconds <= 0:
        return 0
    return max(60.0, seconds)


def _release_gate_watchdog_interval() -> float:
    if not _autonomy_enabled():
        return 0
    try:
        seconds = float(os.environ.get("CODEX_WEB_RELEASE_GATE_WATCHDOG_SECONDS") or "300")
    except ValueError:
        return 300.0
    if seconds <= 0:
        return 0
    return max(60.0, seconds)


def _work_item_sla_watchdog_interval() -> float:
    if not _autonomy_enabled():
        return 0
    try:
        seconds = float(os.environ.get("CODEX_WEB_WORK_ITEM_SLA_WATCHDOG_SECONDS") or "120")
    except ValueError:
        return 120.0
    if seconds <= 0:
        return 0
    return max(30.0, seconds)


def _orchestrator_watchdog_interval() -> float:
    if not _autonomy_enabled():
        return 0
    try:
        seconds = float(os.environ.get("CODEX_WEB_ORCHESTRATOR_WATCHDOG_SECONDS") or "180")
    except ValueError:
        return 180.0
    if seconds <= 0:
        return 0
    return max(30.0, seconds)


def _split_brain_watchdog_interval() -> float:
    if not _autonomy_enabled():
        return 0
    try:
        seconds = float(os.environ.get("CODEX_WEB_SPLIT_BRAIN_WATCHDOG_SECONDS") or "60")
    except ValueError:
        return 60.0
    if seconds <= 0:
        return 0
    return max(15.0, seconds)


def _daemon_health() -> dict[str, Any]:
    now = time.time()
    problems: list[str] = []
    configured_runtime_ids = set(bot_runtime.fingerprints)
    running_runtime_ids = set(bot_runtime.tasks)

    if not codex.proc or codex.proc.poll() is not None:
        problems.append("codex app-server process is not running")
    elif not codex.ready.is_set():
        problems.append("codex app-server is not ready")

    missing_runtimes = configured_runtime_ids - running_runtime_ids
    if missing_runtimes:
        problems.append(f"bot runtime task missing for: {', '.join(sorted(missing_runtimes))}")

    for connection_id, task in bot_runtime.tasks.items():
        if task.done():
            problems.append(f"bot runtime task stopped for: {connection_id}")
            continue
        status = BOT_RUNTIME_STATUS.get(connection_id, {})
        if status.get("status") == "error":
            error_at = float(status.get("lastErrorAt") or status.get("updatedAt") or now)
            if now - error_at > 120:
                problems.append(f"bot runtime has been in error for {int(now - error_at)}s: {connection_id}")

    bound_thread_ids = {binding.thread_id for binding in _load_bot_bindings()}
    terminal_failures = {
        thread_id: list(failures)
        for thread_id, failures in THREAD_TERMINAL_FAILURES.items()
        if thread_id in bound_thread_ids and failures and now - failures[-1][0] < _terminal_failure_window_seconds()
    }
    if terminal_failures:
        problems.append(f"terminal turn failures unresolved for {len(terminal_failures)} bound thread(s)")

    queues = _load_turn_queues()
    stale_queues = {
        thread_id: len(items)
        for thread_id, items in queues.items()
        if items and now - min(item.created_at for item in items) > 900
    }
    if stale_queues:
        problems.append(f"queued turns have waited over 900s for {len(stale_queues)} thread(s)")

    recent_delivery_failures = [
        event
        for event in _recent_bot_events(120)
        if now - float(event.get("created_at") or 0) < 300
        and isinstance(event.get("delivery"), dict)
        and event["delivery"].get("sent") is False
    ]
    if len(recent_delivery_failures) >= 3:
        problems.append(f"{len(recent_delivery_failures)} outbound bot deliveries failed in the last 300s")

    slack_provider_service = getattr(app.state, "slack_provider_service", None)
    slack_provider_health = slack_provider_service.health() if slack_provider_service is not None else {
        "cooldownRemainingSeconds": 0.0,
        "rateLimitFailures": 0,
    }
    slack_backfill_cooldown = float(slack_provider_health["cooldownRemainingSeconds"])
    if int(slack_provider_health["rateLimitFailures"]) >= 2 and slack_backfill_cooldown > 0:
        problems.append(f"Slack backfill rate limited for another {int(slack_backfill_cooldown)}s")
    if GITLAB_SYNC_CONSECUTIVE_FAILURES >= 2:
        problems.append(f"GitLab sync failed {GITLAB_SYNC_CONSECUTIVE_FAILURES} consecutive times")

    return {
        "ok": not problems,
        "problems": problems,
        "codexReady": codex.ready.is_set(),
        "codexPid": codex.proc.pid if codex.proc else None,
        "runtimeConnections": len(bot_runtime.tasks),
        "runtimeStatus": list(BOT_RUNTIME_STATUS.values()),
        "terminalFailureThreads": len(terminal_failures),
        "terminalRecoveryThreads": len(TERMINAL_RECOVERY_TASKS),
        "staleQueueThreads": stale_queues,
        "recentDeliveryFailures": len(recent_delivery_failures),
        "slackBackfillCooldownRemainingSeconds": slack_backfill_cooldown,
        "gitlabSyncConsecutiveFailures": GITLAB_SYNC_CONSECUTIVE_FAILURES,
        "gitlabSyncLastError": GITLAB_SYNC_LAST_ERROR,
        "gitlabSyncLastErrorAt": GITLAB_SYNC_LAST_ERROR_AT or None,
        "gitlabSyncLastSuccessAt": GITLAB_SYNC_LAST_SUCCESS_AT or None,
    }


def _static_version() -> str:
    mtimes = [
        path.stat().st_mtime
        for path in (STATIC_DIR / "index.html", STATIC_DIR / "app.js", STATIC_DIR / "styles.css")
        if path.exists()
    ]
    mtime_version = str(int(max(mtimes) if mtimes else time.time()))
    with contextlib.suppress(Exception):
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=DATA_DIR.parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if commit:
            return f"{commit}-{mtime_version}"
    return mtime_version


def _tail_text_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not path.exists():
        return []
    block_size = 64 * 1024
    chunks: deque[bytes] = deque()
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and newline_count <= limit:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.appendleft(chunk)
            newline_count += chunk.count(b"\n")
    return b"".join(chunks).decode("utf-8", errors="replace").splitlines()[-limit:]


def _recent_bot_events(limit: int = 80) -> list[dict[str, Any]]:
    if not BOTS_EVENTS_FILE.exists():
        return []
    lines = _tail_text_lines(BOTS_EVENTS_FILE, max(1, min(limit, 300)))
    events: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(Exception):
            events.append(json.loads(line))
    return events


def _thread_recent_activity_age_seconds(thread_id: str | None, *, limit: int = 200) -> float | None:
    if not thread_id:
        return None
    now = time.time()
    for event in reversed(_recent_bot_events(limit)):
        if event.get("thread_id") != thread_id:
            continue
        if event.get("type") not in {
            "turn_started",
            "queued_turn_started",
            "outbound_ready",
            "inbound_turn_started",
            "inbound_turn_steered",
            "owner_work_watchdog_dispatched",
            "release_gate_watchdog_dispatched",
        }:
            continue
        created_at = event.get("created_at")
        if isinstance(created_at, (int, float)):
            return max(0.0, now - float(created_at))
    return None


def _thread_recent_event_count(
    thread_id: str | None,
    event_types: set[str],
    *,
    within_seconds: float = 1800.0,
    limit: int = 400,
) -> int:
    if not thread_id:
        return 0
    now = time.time()
    count = 0
    for event in reversed(_recent_bot_events(limit)):
        if event.get("thread_id") != thread_id:
            continue
        if event.get("type") not in event_types:
            continue
        created_at = event.get("created_at")
        if not isinstance(created_at, (int, float)):
            continue
        if (now - float(created_at)) > within_seconds:
            continue
        count += 1
    return count


def _queued_turn_public(queued: QueuedTurn) -> dict[str, Any]:
    preview = queued.message.replace("\n", " ")
    if len(preview) > 180:
        preview = f"{preview[:180]}..."
    return {
        "id": queued.id,
        "threadId": queued.thread_id,
        "projectId": queued.project_id,
        "source": queued.source,
        "attempts": queued.attempts,
        "createdAt": queued.created_at,
        "messagePreview": preview,
        "replyTarget": queued.reply_target.model_dump() if queued.reply_target else None,
    }


def _binding_public(binding: BotBinding) -> dict[str, Any]:
    item = binding.model_dump()
    item["prefix"] = _binding_prefix(binding)
    item["report_name"] = _binding_report_name(binding)
    item["active"] = _thread_is_active(binding.thread_id)
    item["queueDepth"] = _thread_queue_depth(binding.thread_id)
    if binding.provider == "slack":
        item["slack_icon"] = _slack_reply_icon(binding)
        item["slack_username"] = _slack_reply_username(binding)
    return item


def _route_test_message(payload: BotRouteTest) -> BotInboundMessage:
    return BotInboundMessage(
        provider=payload.provider,
        external_conversation_id=payload.external_conversation_id,
        project_id=payload.project_id,
        text=payload.text,
        external_thread_id=payload.external_thread_id,
        message_id=payload.message_id,
    )


def _preview_bot_route(payload: BotRouteTest) -> dict[str, Any]:
    message = _route_test_message(payload)
    provider = message.provider.lower()
    if provider not in {"slack", "telegram"}:
        raise HTTPException(status_code=400, detail="Provider must be slack or telegram")
    bindings = _bindings_for_connection(provider, message.external_conversation_id)
    project_id = message.project_id or (bindings[0].project_id if bindings else "home")
    _project(project_id)
    steer_now, route_message = _steer_route_message(message)
    binding: BotBinding | None = None
    routed_text = route_message.text
    route_error = False
    route_source = "none"
    would_clone = False
    master_catch_all = not steer_now and _has_single_master_binding(bindings)
    prefer_external_thread = (
        not steer_now
        and not master_catch_all
        and not _is_top_level_external_message(message)
    )

    exact_binding = None if not prefer_external_thread else _binding_for_external_target(
        provider,
        project_id,
        message.external_conversation_id,
        message.external_thread_id,
    )
    if exact_binding is not None:
        binding = exact_binding
        would_clone = exact_binding.external_conversation_id != message.external_conversation_id
        route_source = "external-thread"
    else:
        binding, routed_text, route_error = _resolve_bot_binding(
            bindings,
            route_message,
            prefer_external_thread=prefer_external_thread,
            allow_master_fallback=not steer_now,
            allow_bare_prefix=steer_now,
        )
        if binding:
            route_source = "connection-prefix-or-primary"

    if binding is None:
        cross_binding, cross_text, cross_ambiguous = _cross_channel_binding_for_message(
            provider,
            project_id,
            bindings,
            route_message,
            allow_bare_prefix=steer_now,
        )
        if cross_binding is not None:
            binding = cross_binding
            routed_text = cross_text
            route_error = False
            would_clone = cross_binding.external_conversation_id != message.external_conversation_id
            route_source = "cross-channel-prefix-or-primary"
        elif cross_ambiguous:
            route_error = True

    available_prefixes = [_binding_prefix(binding) for binding in bindings if _binding_prefix(binding)]
    if not available_prefixes:
        available_prefixes = [
            _binding_prefix(binding)
            for binding in _bindings_for_project(provider, project_id)
            if _binding_prefix(binding)
        ]

    if route_error:
        return {
            "ok": False,
            "ambiguous": True,
            "projectId": project_id,
            "provider": provider,
            "externalConversationId": message.external_conversation_id,
            "availablePrefixes": sorted(set(available_prefixes)),
            "steer": steer_now,
        }

    if binding is None:
        return {
            "ok": True,
            "wouldCreateThread": True,
            "projectId": project_id,
            "provider": provider,
            "externalConversationId": message.external_conversation_id,
            "routedText": routed_text,
            "routeSource": route_source,
            "steer": steer_now,
        }

    active = _thread_is_active(binding.thread_id)
    queue_depth = _thread_queue_depth(binding.thread_id)
    return {
        "ok": True,
        "wouldCreateThread": False,
        "wouldCloneBinding": would_clone,
        "projectId": binding.project_id,
        "provider": provider,
        "externalConversationId": message.external_conversation_id,
        "bindingId": binding.id,
        "threadId": binding.thread_id,
        "threadName": binding.thread_name,
        "prefix": _binding_prefix(binding),
        "routedText": routed_text,
        "routeSource": route_source,
        "steer": steer_now,
        "active": active,
        "queueDepth": queue_depth,
        "wouldQueue": not steer_now and (active or queue_depth > 0),
    }


def _verify_gitlab_webhook(request: Request) -> None:
    expected = os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET") or os.environ.get("GITLAB_WEBHOOK_SECRET")
    if not expected:
        return
    received = request.headers.get("x-gitlab-token") or ""
    if not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook token")


































def _gitlab_api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    token = _gitlab_api_token()
    if not token:
        raise RuntimeError("GitLab token is not configured for Support ServiceDesk sweep")
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
    url = f"{_gitlab_api_base_url()}/api/v4/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitLab API returned HTTP {exc.code} for {path}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitLab API request failed for {path}: {exc.reason}") from exc




def _support_servicedesk_sweep_payloads() -> list[dict[str, Any]]:
    project = _support_servicedesk_sweep_project()
    encoded_project = urllib.parse.quote(project, safe="")
    project_payload = _gitlab_api_get(f"projects/{encoded_project}")
    project_path = str(project_payload.get("path_with_namespace") or project)
    project_id = project_payload.get("id") or project
    lookback_hours = _support_servicedesk_sweep_lookback_hours()
    created_after = None
    if lookback_hours:
        created_after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - (lookback_hours * 3600)))
    issues = _gitlab_api_get(
        f"projects/{encoded_project}/issues",
        {
            "state": "opened",
            "created_after": created_after,
            "order_by": "created_at",
            "sort": "asc",
            "per_page": 100,
        },
    )
    if not isinstance(issues, list):
        return []
    return [_issue_to_support_servicedesk_payload(issue, project_path, project_id) for issue in issues if isinstance(issue, dict)]


























def _gitlab_group_issues(
    project_id: str,
    project_settings: GitLabProjectRoutingSettings,
    *,
    labels: list[str] | None = None,
    state: str = "opened",
) -> list[dict[str, Any]]:
    token = _gitlab_token_for_project(project_id)
    group = _gitlab_group_path(project_settings)
    if not token or not group:
        return []
    query = {
        "state": state,
        "per_page": "100",
    }
    if labels:
        query["labels"] = ",".join(labels)
    url = f"{GITLAB_API_BASE}/groups/{urllib.parse.quote_plus(group)}/issues?{urllib.parse.urlencode(query)}"
    response = _get_json(url, headers={"PRIVATE-TOKEN": token})
    return response if isinstance(response, list) else []


def _sync_work_item_states_from_gitlab() -> dict[str, int]:
    synced = 0
    seen_refs: set[str] = set()
    settings = _load_gitlab_routing_settings()
    for project_id, project_settings in settings.projects.items():
        if not project_settings.enabled:
            continue
        issues = _gitlab_group_issues(project_id, project_settings, state="opened")
        for issue in issues:
            state = _upsert_work_item_state_from_gitlab_issue(issue, project_id=project_id)
            if not state:
                continue
            synced += 1
            seen_refs.add(state.ref)
    return {"synced": synced, "refs": len(seen_refs)}








def _work_item_stage_from_gitlab_payload(payload: dict[str, Any]) -> str:
    labels = _gitlab_label_names(payload)
    status_label = _current_status_label(labels)
    attrs = payload.get("object_attributes") or {}
    state = str(attrs.get("state") or attrs.get("status") or "").strip().lower()
    return _gitlab_stage_from_projection(state_name=state, status_label=status_label)


async def _dispatch_structured_handoff_to_recipient(state: WorkItemState, *, source: str) -> None:
    if not state.project_id or not state.handoff or state.handoff.status != "pending":
        return
    recipient = _coerce_owner(state.handoff.to_agent)
    if not recipient:
        return
    binding = _binding_for_agent(
        recipient,
        state.project_id,
        preferred_conversation_id=HANDOFF_COORDINATION_CHANNEL,
    )
    if not binding:
        _append_bot_event(
            {
                "type": "work_item_handoff_dispatch_skipped",
                "ref": state.ref,
                "agent": recipient,
                "reason": "no_binding",
                "source": source,
            }
        )
        return
    binding = await _replace_nonperforming_thread_if_needed(binding, source)
    result = await _dispatch_event_to_binding(binding, _work_item_dispatch_text(state), source)
    _append_bot_event(
        {
            "type": "work_item_handoff_dispatched",
            "ref": state.ref,
            "thread_id": binding.thread_id,
            "agent": recipient,
            "source": source,
            "result": result,
        }
    )


def _schedule_structured_handoff_dispatch(state: WorkItemState, *, source: str) -> None:
    async def run() -> None:
        try:
            await _dispatch_structured_handoff_to_recipient(state, source=source)
        except Exception as exc:
            _append_bot_event(
                {
                    "type": "work_item_handoff_dispatch_failed",
                    "ref": state.ref,
                    "source": source,
                    "error": _truncate_text(str(getattr(exc, "detail", exc)), 500),
                }
            )

    asyncio.create_task(run())


async def _run_handoff_continuity_check(
    ref: str,
    *,
    expected_recipient: str | None,
    expected_requested_at: float | None,
    source: str,
) -> None:
    delay = _handoff_continuity_delay_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        state = _work_item_state(ref)
    except HTTPException:
        return
    handoff = state.handoff
    if not handoff or handoff.status != "pending":
        return
    recipient = _coerce_owner(handoff.to_agent)
    if expected_recipient and recipient != expected_recipient:
        return
    if expected_requested_at is not None and handoff.requested_at != expected_requested_at:
        return
    if not state.project_id or not recipient:
        return
    binding = _binding_for_agent(
        recipient,
        state.project_id,
        preferred_conversation_id=HANDOFF_COORDINATION_CHANNEL,
    )
    if not binding:
        return
    if _thread_is_active(binding.thread_id) or _thread_queue_depth(binding.thread_id) or _thread_recently_active(binding.thread_id):
        return
    binding = await _replace_nonperforming_thread_if_needed(binding, source)
    dispatch_key = f"handoff-continuity:{ref}:{binding.thread_id}:{recipient}:{handoff.requested_at}"
    if not _watchdog_dispatch_allowed(dispatch_key):
        return
    _record_watchdog_dispatch(dispatch_key)
    result = await _dispatch_event_to_binding(binding, _work_item_dispatch_text(state), source)
    _append_bot_event(
        {
            "type": "work_item_handoff_continuity_dispatched",
            "ref": ref,
            "thread_id": binding.thread_id,
            "agent": recipient,
            "result": result,
        }
    )


def _schedule_handoff_continuity_check(
    state: WorkItemState,
    *,
    source: str,
) -> None:
    handoff = state.handoff
    if not handoff or handoff.status != "pending":
        return
    recipient = _coerce_owner(handoff.to_agent)
    if not recipient:
        return
    existing = HANDOFF_CONTINUITY_TASKS.get(state.ref)
    if existing and not existing.done():
        existing.cancel()

    async def run() -> None:
        try:
            await _run_handoff_continuity_check(
                state.ref,
                expected_recipient=recipient,
                expected_requested_at=handoff.requested_at,
                source=source,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _append_bot_event(
                {
                    "type": "handoff_continuity_check_failed",
                    "ref": state.ref,
                    "source": source,
                    "error": _truncate_text(str(getattr(exc, "detail", exc)), 500),
                }
            )
        finally:
            current = HANDOFF_CONTINUITY_TASKS.get(state.ref)
            if current is task:
                HANDOFF_CONTINUITY_TASKS.pop(state.ref, None)

    task = asyncio.create_task(run())
    HANDOFF_CONTINUITY_TASKS[state.ref] = task


def _actionable_owner_dispatch_stage(stage: str | None) -> bool:
    return stage in {
        "implementation_active",
        "failed_with_action_owner",
        "ready_for_validation",
        "validation_running",
        "ready_to_close",
    }


async def _dispatch_actionable_owner_to_responsible_thread(
    state: WorkItemState,
    *,
    source: str,
    actor: str | None = None,
) -> None:
    if state.current_stage == "closed" or state.closed_at:
        return
    if not _actionable_owner_dispatch_stage(state.current_stage):
        return
    if state.handoff and state.handoff.status == "pending":
        return
    owner = _coerce_owner(state.current_owner or state.next_owner)
    if not owner:
        return
    if _coerce_owner(actor) == owner:
        return
    binding = _binding_for_agent(
        owner,
        state.project_id or "",
        preferred_conversation_id=HANDOFF_COORDINATION_CHANNEL,
    )
    if not binding:
        return
    if _thread_is_active(binding.thread_id) or _thread_queue_depth(binding.thread_id):
        return
    binding = await _replace_nonperforming_thread_if_needed(binding, source)
    dispatch_key = f"work-item-owner-progress:{state.ref}:{binding.thread_id}:{owner}:{state.current_stage}"
    if not _watchdog_dispatch_allowed(dispatch_key):
        return
    _record_watchdog_dispatch(dispatch_key)
    result = await _dispatch_event_to_binding(binding, _work_item_dispatch_text(state), source)
    _append_bot_event(
        {
            "type": "work_item_owner_progress_dispatched",
            "ref": state.ref,
            "thread_id": binding.thread_id,
            "agent": owner,
            "current_stage": state.current_stage,
            "source": source,
            "actor": actor,
            "result": result,
        }
    )


def _schedule_actionable_owner_dispatch(
    state: WorkItemState,
    *,
    source: str,
    actor: str | None = None,
) -> None:
    async def run() -> None:
        try:
            await _dispatch_actionable_owner_to_responsible_thread(
                state,
                source=source,
                actor=actor,
            )
        except Exception as exc:
            _append_bot_event(
                {
                    "type": "work_item_owner_progress_dispatch_failed",
                    "ref": state.ref,
                    "source": source,
                    "actor": actor,
                    "error": _truncate_text(str(getattr(exc, "detail", exc)), 500),
                }
            )

    asyncio.create_task(run())


async def _run_actionable_owner_continuity_check(
    ref: str,
    *,
    expected_owner: str | None,
    expected_stage: str | None,
    source: str,
) -> None:
    delay = _actionable_owner_continuity_delay_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        state = _work_item_state(ref)
    except HTTPException:
        return
    if state.current_stage == "closed" or state.closed_at:
        return
    if not _actionable_owner_dispatch_stage(state.current_stage):
        return
    current_owner = _coerce_owner(state.current_owner or state.next_owner)
    if expected_owner and current_owner != expected_owner:
        return
    if expected_stage and state.current_stage != expected_stage:
        return
    await _dispatch_actionable_owner_to_responsible_thread(
        state,
        source=source,
        actor=None,
    )


def _schedule_actionable_owner_continuity_check(
    state: WorkItemState,
    *,
    source: str,
) -> None:
    if state.current_stage == "closed" or state.closed_at:
        return
    if not _actionable_owner_dispatch_stage(state.current_stage):
        return
    owner = _coerce_owner(state.current_owner or state.next_owner)
    if not owner:
        return
    existing = ACTIONABLE_OWNER_CONTINUITY_TASKS.get(state.ref)
    if existing and not existing.done():
        existing.cancel()

    async def run() -> None:
        try:
            await _run_actionable_owner_continuity_check(
                state.ref,
                expected_owner=owner,
                expected_stage=state.current_stage,
                source=source,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _append_bot_event(
                {
                    "type": "actionable_owner_continuity_check_failed",
                    "ref": state.ref,
                    "source": source,
                    "error": _truncate_text(str(getattr(exc, "detail", exc)), 500),
                }
            )
        finally:
            current = ACTIONABLE_OWNER_CONTINUITY_TASKS.get(state.ref)
            if current is task:
                ACTIONABLE_OWNER_CONTINUITY_TASKS.pop(state.ref, None)

    task = asyncio.create_task(run())
    ACTIONABLE_OWNER_CONTINUITY_TASKS[state.ref] = task


def _format_gitlab_event_prompt(payload: dict[str, Any], agent: str | None) -> str:
    attrs = payload.get("object_attributes") or {}
    kind = str(payload.get("object_kind") or payload.get("event_name") or "event")
    action = attrs.get("action") or attrs.get("state") or attrs.get("status") or ""
    labels = _gitlab_label_names(payload)
    url = _gitlab_url(payload)
    lines = [
        f"GitLab event received for {agent or 'the project'}: {_gitlab_reference(payload)}",
        f"Kind/status: {kind}{f' / {action}' if action else ''}",
    ]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    if url:
        lines.append(f"URL: {url}")
    if kind == "pipeline":
        lines.append(
            "Pipeline details: "
            + ", ".join(
                part
                for part in (
                    f"ref={attrs.get('ref')}" if attrs.get("ref") else "",
                    f"sha={str(attrs.get('sha') or '')[:12]}" if attrs.get("sha") else "",
                    f"duration={attrs.get('duration')}" if attrs.get("duration") is not None else "",
                )
                if part
            )
        )
    lines.extend(
        [
            "",
            "Handle this event-driven update within your directive. Inspect the linked GitLab item/MR/pipeline only as needed.",
            "Before ending the turn, reconcile the affected work item through the codex-web `/api/work-items` handoff/ack/progress endpoints as applicable.",
            "Do not poll GitLab for generic queue state in this turn. Keep any Slack/GitLab update concise and avoid repeating prior evidence.",
        ]
    )
    return "\n".join(lines)


def _format_gitlab_event_notice(payload: dict[str, Any], agent: str | None, result: dict[str, Any]) -> str:
    attrs = payload.get("object_attributes") or {}
    kind = str(payload.get("object_kind") or payload.get("event_name") or "event").replace("_", " ")
    action = attrs.get("action") or attrs.get("state") or attrs.get("status") or ""
    labels = _gitlab_label_names(payload)
    url = _gitlab_url(payload)
    route_name = agent or "project"
    dispatch_state = "queued" if result.get("queued") else "started"
    lines = [
        f"GitLab event: {_gitlab_reference(payload)}",
        f"Kind/status: {kind}{f' / {action}' if action else ''}",
        f"Routed to: {route_name} ({dispatch_state})",
    ]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    if url:
        lines.append(f"URL: {url}")
    return "\n".join(lines)


async def _send_gitlab_event_notice(
    binding: BotBinding,
    payload: dict[str, Any],
    agent: str | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    if binding.provider != "slack":
        return {"sent": False, "reason": "unsupported_provider"}
    delivery = await _send_bot_outbound(
        binding,
        _format_gitlab_event_notice(payload, agent, result),
        username="GitLab",
    )
    _remember_bot_delivery_target(binding, delivery)
    _append_bot_event(
        {
            "type": "gitlab_notice_sent",
            "thread_id": binding.thread_id,
            "provider": binding.provider,
            "external_conversation_id": binding.external_conversation_id,
            "agent": agent,
            "delivery": delivery,
        }
    )
    return delivery






def _work_item_dispatch_text(state: WorkItemState) -> str:
    findings_suffix = ""
    if state.blocking_findings:
        findings_suffix = " Supporting findings: " + "; ".join(state.blocking_findings[:3]) + "."
    if state.handoff and state.handoff.status == "pending":
        return (
            f"{state.handoff.to_agent}: structured handoff pending for {state.ref}. "
            f"Acknowledge receipt and intent to process in C0B9M89AHCY now. "
            f"Expected action: {state.handoff.expected_action or state.next_action or 'process the handoff'}.{findings_suffix} "
            f"If you cannot accept it, emit one exact blocker immediately. "
            f"Use `/api/work-items/{urllib.parse.quote(state.ref, safe='')}/ack` before you stop."
        )
    if (
        state.handoff
        and state.handoff.status == "accepted"
        and _coerce_owner(state.current_owner) == _coerce_owner(state.handoff.to_agent)
    ):
        return (
            f"{state.current_owner or state.next_owner or 'owner'}: accepted handoff is live for {state.ref}. "
            f"Current stage: {state.current_stage}. "
            f"Next action: {state.next_action or 'continue the owned lane now'}.{findings_suffix} "
            f"Do not leave the lane parked after acknowledgement. "
            f"Record concrete progress, an exact blocker, or an exact handoff in codex-web before you stop."
        )
    if state.current_stage in {"ready_for_validation", "validation_running", "ready_to_close"}:
        return (
            f"{state.current_owner or state.next_owner or 'owner'}: release/validation lane for {state.ref}. "
            f"Current stage: {state.current_stage}. "
            f"Next action: {state.next_action or 'acknowledge and process the release-side lane'}.{findings_suffix} "
            f"Close the lane or emit one exact blocker in C0B9M89AHCY. "
            f"Reconcile the structured work-item progress before ending the turn."
        )
    return (
        f"{state.current_owner or state.next_owner or 'owner'}: owned-work SLA triggered for {state.ref}. "
        f"Current stage: {state.current_stage}. "
        f"Next action: {state.next_action or 'state the exact next action and continue the item'}.{findings_suffix} "
        f"No passive waiting is allowed. Record the resulting progress or blocker in codex-web before you stop."
    )




def _owner_activity_timestamp(state: WorkItemState) -> float:
    if state.last_owner_activity_at is not None:
        return state.last_owner_activity_at
    if (
        state.handoff
        and state.handoff.status == "accepted"
        and _coerce_owner(state.current_owner) == _coerce_owner(state.handoff.to_agent)
        and state.handoff.acknowledged_at is not None
    ):
        return state.handoff.acknowledged_at
    return state.last_meaningful_update_at


def _human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d" if hrs == 0 else f"{days}d {hrs}h"


def _orchestrator_watch_reason(state: WorkItemState, *, now: float) -> tuple[str, float] | None:
    if state.current_stage == "closed" or state.closed_at:
        return None
    if _work_item_split_brain_findings(state):
        return ("split_brain", now - state.updated_at)
    if state.handoff and state.handoff.status == "pending":
        pending_age = now - state.handoff.requested_at
        if pending_age >= min(_work_item_handoff_timeout_seconds(), 300.0):
            return ("pending_handoff", pending_age)
    owner = _coerce_owner(state.current_owner or state.next_owner)
    if not owner:
        return ("unowned", now - state.updated_at)
    age = now - state.last_meaningful_update_at
    if state.current_stage == "failed_with_action_owner":
        return ("blocked_lane", age)
    if age >= _work_item_sla_threshold_seconds(state):
        return ("stale_lane", age)
    return None


def _orchestrator_watchdog_candidates(project_id: str) -> list[tuple[str, WorkItemState, float]]:
    now = time.time()
    candidates: list[tuple[str, WorkItemState, float]] = []
    for state in _load_work_item_states().values():
        if state.project_id != project_id:
            continue
        decision = _orchestrator_watch_reason(state, now=now)
        if not decision:
            continue
        reason, age = decision
        candidates.append((reason, state, age))
    candidates.sort(
        key=lambda item: (
            0 if item[1].release_gate else 1,
            0 if item[0] == "unowned" else 1,
            -(item[2]),
            item[1].ref,
        )
    )
    return candidates


def _format_orchestrator_watchdog_prompt(project_id: str, items: list[tuple[str, WorkItemState, float]]) -> str:
    project = _project(project_id)
    lines = [
        "Orchestrator: autonomous follow-up sweep.",
        f"Project: {project.name} ({project.path})",
        "Keep the work-item loop closed until each listed item is assigned, acknowledged, advanced, or closed.",
        "",
        "Required in this turn:",
        "1. Push the named owner or next owner if follow-up is needed.",
        "2. Record the resulting ownership/progress decision through codex-web `/api/work-items` before you stop.",
        "3. Do not leave any listed item without one exact next action.",
        "",
        "Items needing orchestration:",
    ]
    for index, (reason, state, age) in enumerate(items[:8], start=1):
        owner = state.current_owner or state.next_owner or "unassigned"
        action = state.next_action or "set one exact next action"
        lines.append(
            f"{index}. {state.ref} | trigger={reason} | owner={owner} | stage={state.current_stage} | age={_human_duration(age)}"
        )
        lines.append(f"   next_action={action}")
        if state.handoff and state.handoff.status == "pending":
            lines.append(
                "   pending_handoff="
                + f"{state.handoff.from_agent}->{state.handoff.to_agent} for {_human_duration(time.time() - state.handoff.requested_at)}"
            )
        if state.blocker:
            lines.append(f"   blocker={state.blocker}")
        if state.blocking_findings:
            lines.append("   blocking_findings=" + " | ".join(state.blocking_findings[:3]))
    if len(items) > 8:
        lines.append(f"... plus {len(items) - 8} more stale items.")
    lines.append("")
    lines.append("If a listed item is already moving, reconcile the structured state anyway and explicitly state who owns the next step.")
    return "\n".join(lines)


def _split_brain_watchdog_candidates(project_id: str) -> list[tuple[WorkItemState, list[str]]]:
    candidates: list[tuple[WorkItemState, list[str]]] = []
    for state in _load_work_item_states().values():
        if state.project_id != project_id or state.current_stage == "closed" or state.closed_at:
            continue
        findings = _work_item_split_brain_findings(state)
        if findings:
            candidates.append((state, findings))
    candidates.sort(key=lambda item: (0 if item[0].release_gate else 1, item[0].ref))
    return candidates


def _format_split_brain_watchdog_prompt(project_id: str, items: list[tuple[WorkItemState, list[str]]]) -> str:
    project = _project(project_id)
    lines = [
        "Orchestrator: continuous split-brain monitor triggered.",
        f"Project: {project.name} ({project.path})",
        "Reconcile each item to one canonical owner, one canonical stage, and one canonical next action in this turn.",
        "",
        "Items with live owner/stage/handoff drift:",
    ]
    for index, (state, findings) in enumerate(items[:8], start=1):
        lines.append(
            f"{index}. {state.ref} | owner={state.current_owner or 'unassigned'} | stage={state.current_stage} | next_owner={state.next_owner or 'none'}"
        )
        for finding in findings:
            lines.append(f"   - {finding}")
        if state.next_action:
            lines.append(f"   next_action={state.next_action}")
        if state.blocking_findings:
            lines.append("   blocking_findings=" + " | ".join(state.blocking_findings[:3]))
    if len(items) > 8:
        lines.append(f"... plus {len(items) - 8} more split-brain items.")
    lines.append("")
    lines.append("Record the reconciliation through codex-web `/api/work-items` before you stop.")
    return "\n".join(lines)














def _diagnostic_snapshot(project_id: str | None = None) -> dict[str, Any]:
    bindings = _load_bot_bindings()
    if project_id:
        _project(project_id)
        bindings = [binding for binding in bindings if binding.project_id == project_id]
    queues = _load_turn_queues()
    active_turns = _load_active_turns()
    slack_provider_service = getattr(app.state, "slack_provider_service", None)
    slack_provider_health = slack_provider_service.health() if slack_provider_service is not None else {
        "intervalSeconds": 0.0,
        "running": False,
        "cooldownRemainingSeconds": 0.0,
        "cooldownUntil": None,
    }
    return {
        "generatedAt": time.time(),
        "version": _static_version(),
        "status": {
            "ok": codex.ready.is_set(),
            "pid": codex.proc.pid if codex.proc else None,
            "error": None if codex.ready.is_set() else codex.last_error,
            "pendingApprovals": len(codex.pending_approvals),
            "activeTurns": len(active_turns),
            "queuedTurns": sum(len(items) for items in queues.values()),
            "ownerWorkWatchdogIntervalSeconds": _owner_work_watchdog_interval(),
            "ownerWorkWatchdogRunning": bool(OWNER_WORK_WATCHDOG_TASK and not OWNER_WORK_WATCHDOG_TASK.done()),
            "releaseGateWatchdogIntervalSeconds": _release_gate_watchdog_interval(),
            "releaseGateWatchdogRunning": bool(RELEASE_GATE_WATCHDOG_TASK and not RELEASE_GATE_WATCHDOG_TASK.done()),
            "workItemSlaWatchdogIntervalSeconds": _work_item_sla_watchdog_interval(),
            "workItemSlaWatchdogRunning": bool(WORK_ITEM_SLA_TASK and not WORK_ITEM_SLA_TASK.done()),
            "orchestratorWatchdogIntervalSeconds": _orchestrator_watchdog_interval(),
            "orchestratorWatchdogRunning": bool(ORCHESTRATOR_WATCHDOG_TASK and not ORCHESTRATOR_WATCHDOG_TASK.done()),
            "splitBrainWatchdogIntervalSeconds": _split_brain_watchdog_interval(),
            "splitBrainWatchdogRunning": bool(SPLIT_BRAIN_WATCHDOG_TASK and not SPLIT_BRAIN_WATCHDOG_TASK.done()),
            "threadMessageLimit": _default_thread_message_limit(),
            "slackBackfillIntervalSeconds": slack_provider_health["intervalSeconds"],
            "slackBackfillRunning": slack_provider_health["running"],
            "slackBackfillCooldownRemainingSeconds": slack_provider_health["cooldownRemainingSeconds"],
            "slackBackfillCooldownUntil": slack_provider_health["cooldownUntil"],
        },
        "health": _daemon_health(),
        "projects": [project.model_dump() for project in _load_projects()],
        "threadIndex": [thread.model_dump() for thread in _load_thread_index()],
        "activeTurns": [active.model_dump() for active in active_turns.values()],
        "queues": {
            thread_id: [_queued_turn_public(queued) for queued in items]
            for thread_id, items in queues.items()
            if not project_id or any(queued.project_id == project_id for queued in items)
        },
        "queueTasks": {
            thread_id: {
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for thread_id, task in QUEUE_DRAIN_TASKS.items()
        },
        "connections": [
            {
                **_bot_connection_public(connection),
                "runtime": BOT_RUNTIME_STATUS.get(connection.id),
                "runtimeTaskRunning": connection.id in bot_runtime.tasks and not bot_runtime.tasks[connection.id].done(),
            }
            for connection in _load_bot_connections()
            if not project_id or connection.project_id == project_id
        ],
        "bindings": [_binding_public(binding) for binding in bindings],
        "agentChannelPresence": _agent_channel_presence_payload(_load_agent_channel_presence_settings()),
        "replyTargets": {
            key: target.model_dump()
            for key, target in _load_bot_reply_targets().items()
            if not project_id or target.thread_id in {binding.thread_id for binding in bindings}
        },
        "deliveryTargets": {
            key: target.model_dump()
            for key, target in _load_bot_delivery_targets().items()
            if not project_id or target.thread_id in {binding.thread_id for binding in bindings}
        },
        "workItemStates": [
            _work_item_state_public(state)
            for state in sorted(_load_work_item_states().values(), key=lambda item: item.updated_at, reverse=True)[:200]
            if not project_id or state.project_id == project_id
        ],
        "recentBotEvents": _recent_bot_events(),
    }




def _watchdog_dispatch_cooldown_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_WATCHDOG_DISPATCH_COOLDOWN_SECONDS") or "120")
    except ValueError:
        return 120.0
    return max(5.0, seconds)


def _watchdog_recent_activity_grace_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_WATCHDOG_RECENT_ACTIVITY_GRACE_SECONDS") or "300")
    except ValueError:
        return 300.0
    return max(30.0, seconds)


def _watchdog_replacement_threshold() -> int:
    try:
        value = int(os.environ.get("CODEX_WEB_WATCHDOG_REPLACEMENT_THRESHOLD") or "3")
    except ValueError:
        return 3
    return max(2, value)


def _actionable_owner_continuity_delay_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_ACTIONABLE_OWNER_CONTINUITY_DELAY_SECONDS") or "45")
    except ValueError:
        return 45.0
    return max(5.0, seconds)


def _handoff_continuity_delay_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_HANDOFF_CONTINUITY_DELAY_SECONDS") or "45")
    except ValueError:
        return 45.0
    return max(5.0, seconds)


def _native_recovery_schedule_cooldown_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_NATIVE_RECOVERY_SCHEDULE_COOLDOWN_SECONDS") or "30")
    except ValueError:
        return 30.0
    return max(1.0, seconds)


def _watchdog_dispatch_allowed(key: str, *, now: float | None = None) -> bool:
    ts = now or time.time()
    last = WATCHDOG_DISPATCH_TIMES.get(key)
    if last is None:
        return True
    return (ts - last) >= _watchdog_dispatch_cooldown_seconds()


def _record_watchdog_dispatch(key: str, *, now: float | None = None) -> None:
    WATCHDOG_DISPATCH_TIMES[key] = now or time.time()


def _thread_recently_active(thread_id: str | None) -> bool:
    age = _thread_recent_activity_age_seconds(thread_id)
    if age is None:
        return False
    return age < _watchdog_recent_activity_grace_seconds()


async def _replace_nonperforming_thread_if_needed(binding: BotBinding, reason: str) -> BotBinding:
    if not binding.thread_id:
        return binding
    if _thread_is_active(binding.thread_id) or _thread_queue_depth(binding.thread_id):
        return binding
    if _thread_recently_active(binding.thread_id):
        return binding
    dispatch_count = _thread_recent_event_count(
        binding.thread_id,
        {
            "owner_work_watchdog_dispatched",
            "release_gate_watchdog_dispatched",
            "work_item_sla_dispatched",
            "work_item_handoff_watchdog_dispatched",
        },
    )
    if dispatch_count < _watchdog_replacement_threshold():
        return binding
    replacement = await _replace_stale_bot_thread(binding, f"autonomous replacement after {dispatch_count} watchdog dispatches ({reason})")
    _append_bot_event(
        {
            "type": "autonomous_thread_replaced",
            "reason": reason,
            "old_thread_id": binding.thread_id,
            "new_thread_id": replacement.thread_id,
            "logical_name": _logical_binding_name(binding),
        }
    )
    return replacement


def _schedule_native_recovery_cycles(*, reason: str = "manual") -> None:
    global NATIVE_RECOVERY_LAST_SCHEDULED_AT
    if not _autonomy_enabled():
        return
    if IS_SHUTTING_DOWN:
        return
    now = time.time()
    if (now - NATIVE_RECOVERY_LAST_SCHEDULED_AT) < _native_recovery_schedule_cooldown_seconds():
        return
    NATIVE_RECOVERY_LAST_SCHEDULED_AT = now
    _append_bot_event({"type": "native_recovery_scheduled", "reason": reason})
    asyncio.create_task(_run_owner_work_watchdog_cycle())
    asyncio.create_task(_run_release_gate_watchdog_cycle())
    asyncio.create_task(_run_work_item_sla_cycle())
    asyncio.create_task(_run_orchestrator_watchdog_cycle())












@app.get("/")
async def index() -> HTMLResponse:
    version = _static_version()
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace('href="static/styles.css"', f'href="static/styles.css?v={version}"')
    html = html.replace('src="static/app.js"', f'src="static/app.js?v={version}"')
    return HTMLResponse(html)


@app.get("/devstatus")
async def devstatus() -> HTMLResponse:
    return HTMLResponse(render_devstatus_html(build_devstatus_context()))


@app.get("/devhealth")
async def devhealth(request: Request) -> HTMLResponse:
    force_refresh = request.query_params.get("refresh") in {"1", "true", "yes"}
    queued_turns = sum(len(items) for items in _load_turn_queues().values())
    status_context = build_devstatus_context(force_refresh=force_refresh)
    return HTMLResponse(
        render_devhealth_html(
            build_devhealth_context(
                _daemon_health(),
                active_turns=len(_load_active_turns()),
                queued_turns=queued_turns,
                status_context=status_context,
                work_item_stats=_devhealth_work_item_stats(),
                refresh_url="/devhealth?refresh=1",
            )
        )
    )


@app.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "hello", "time": time.time()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)






def _codex_verifier_credentials() -> tuple[str, str] | None:
    user = os.environ.get("CODEX_WEB_VERIFIER_USER", "").strip()
    password = os.environ.get("CODEX_WEB_VERIFIER_PASSWORD", "")
    if not user or not password:
        return None
    return user, password


def _basic_auth_credentials(header_value: str | None) -> tuple[str, str] | None:
    if not header_value:
        return None
    scheme, _, encoded = header_value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password










def _agent_channel_presence_payload(settings: AgentChannelPresenceSettings) -> dict[str, Any]:
    return settings.model_dump()






def _gitlab_integration_payload(settings: GitLabRoutingSettings) -> dict[str, Any]:
    return {
        **settings.model_dump(),
        "webhookPath": "/bots/gitlab/events",
        "tokenVerification": bool(
            os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
            or os.environ.get("GITLAB_WEBHOOK_SECRET")
        ),
    }
























































































def _sandbox_policy(mode: str, cwd: str) -> dict[str, Any]:
    if mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    return {
        "type": "workspaceWrite",
        "writableRoots": [cwd],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _approval_result(method: str, decision: str) -> dict[str, Any]:
    accept = decision in {"accept", "acceptForSession", "approved", "approved_for_session"}
    session = decision in {"acceptForSession", "approved_for_session"}
    if method == "item/commandExecution/requestApproval":
        return {"decision": "acceptForSession" if session else ("accept" if accept else "decline")}
    if method == "item/fileChange/requestApproval":
        return {"decision": "acceptForSession" if session else ("accept" if accept else "decline")}
    if method == "execCommandApproval":
        return {"decision": "approved_for_session" if session else ("approved" if accept else "denied")}
    if method == "applyPatchApproval":
        return {"decision": "approved" if accept else "denied"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "session" if session else "turn", "strictAutoReview": not accept}
    return {"decision": "accept" if accept else "decline"}


def main() -> None:
    import uvicorn

    host = os.environ.get("CODEX_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEX_WEB_PORT", "8765"))
    uvicorn.run("server:app", host=host, port=port, reload=False, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
