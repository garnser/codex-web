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
    THREAD_INDEX_FILE,
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
SLACK_BACKFILL_TASK: asyncio.Task[None] | None = None
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
STATE_FILE_LOCKS: dict[str, threading.RLock] = {}
GITLAB_EVENT_IDS: dict[str, float] = {}
WATCHDOG_DISPATCH_TIMES: dict[str, float] = {}
SLACK_BACKFILL_SEEN: set[str] = set()
SLACK_BACKFILL_BAD_THREADS: set[tuple[str, str, str]] = set()
SLACK_BACKFILL_COOLDOWN_UNTIL = 0.0
SLACK_BACKFILL_RATE_LIMIT_FAILURES = 0
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
DEFAULT_THREAD_MESSAGE_LIMIT = 100


hub = EventHub()


def _state_file_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    lock = STATE_FILE_LOCKS.get(key)
    if lock is None:
        lock = threading.RLock()
        STATE_FILE_LOCKS[key] = lock
    return lock


def _atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _state_file_lock(path)
    with lock:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            mode = 0o600 if private else 0o644
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w") as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()


def _reply_target_key(binding: BotBinding) -> str:
    return f"{binding.provider}:{binding.external_conversation_id}:{binding.thread_id}"


def _conversation_target_for_binding(binding: BotBinding) -> BotReplyTarget:
    return BotReplyTarget(
        thread_id=binding.thread_id,
        provider=binding.provider,
        external_conversation_id=binding.external_conversation_id,
        external_thread_id=None,
        message_id=None,
        updated_at=time.time(),
    )


def _external_target_key(provider: str, external_conversation_id: str, external_id: str) -> str:
    return f"{provider}:{external_conversation_id}:external:{external_id}"


def _remember_bot_reply_target(binding: BotBinding, message: BotInboundMessage) -> BotReplyTarget | None:
    if not message.external_thread_id and not message.message_id:
        return None
    targets = _load_bot_reply_targets()
    target = BotReplyTarget(
        thread_id=binding.thread_id,
        provider=binding.provider,
        external_conversation_id=binding.external_conversation_id,
        external_thread_id=message.external_thread_id,
        message_id=message.message_id,
        updated_at=time.time(),
    )
    targets[_reply_target_key(binding)] = target
    for external_id in {message.external_thread_id, message.message_id}:
        if external_id:
            targets[_external_target_key(binding.provider, binding.external_conversation_id, external_id)] = target
    _save_bot_reply_targets(targets)
    return target


def _reply_target_for_binding(binding: BotBinding) -> BotReplyTarget | None:
    targets = _load_bot_reply_targets()
    target = targets.get(_reply_target_key(binding)) or targets.get(binding.thread_id)
    if not target:
        return None
    if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
        return None
    return target


def _delivery_target_for_binding(binding: BotBinding) -> BotReplyTarget | None:
    targets = _load_bot_delivery_targets()
    target = targets.get(_reply_target_key(binding)) or targets.get(binding.thread_id)
    if not target:
        return None
    if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
        return None
    return target


def _active_reply_target_for_binding(binding: BotBinding) -> BotReplyTarget | None:
    active = _load_active_turns().get(binding.thread_id)
    target = active.reply_target if active else None
    if not target:
        return None
    if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
        return None
    return target


def _active_reply_target_for_thread_provider(thread_id: str, provider: str) -> BotReplyTarget | None:
    active = _load_active_turns().get(thread_id)
    target = active.reply_target if active else None
    if target and target.provider == provider:
        return target
    return None


def _target_for_external_thread(
    provider: str,
    external_conversation_id: str,
    external_thread_id: str | None,
) -> BotReplyTarget | None:
    if not external_thread_id:
        return None
    normalized_provider = provider.lower()
    for targets in (_load_bot_reply_targets(), _load_bot_delivery_targets()):
        direct = targets.get(_external_target_key(normalized_provider, external_conversation_id, external_thread_id))
        if direct:
            return direct
        for target in targets.values():
            if target.provider != normalized_provider or target.external_conversation_id != external_conversation_id:
                continue
            if target.external_thread_id == external_thread_id or target.message_id == external_thread_id:
                return target
    return None


def _remember_bot_delivery_target(binding: BotBinding, delivery: dict[str, Any]) -> None:
    response = delivery.get("providerResponse") or {}
    ts = response.get("ts")
    if not delivery.get("sent") or not ts:
        return
    targets = _load_bot_delivery_targets()
    target = BotReplyTarget(
        thread_id=binding.thread_id,
        provider=binding.provider,
        external_conversation_id=binding.external_conversation_id,
        external_thread_id=str(ts),
        message_id=str(ts),
        updated_at=time.time(),
    )
    targets[_reply_target_key(binding)] = target
    targets[_external_target_key(binding.provider, binding.external_conversation_id, str(ts))] = target
    _save_bot_delivery_targets(targets)


def _master_reply_target_for_binding(binding: BotBinding) -> BotReplyTarget | None:
    if binding.is_master:
        return None
    candidates = [
        candidate
        for candidate in _bindings_for_project(binding.provider, binding.project_id)
        if candidate.is_master and candidate.external_conversation_id == binding.external_conversation_id
    ]
    candidates.sort(key=lambda candidate: (candidate.updated_at, candidate.created_at), reverse=True)
    for candidate in candidates:
        target = (
            _active_reply_target_for_binding(candidate)
            or _reply_target_for_binding(candidate)
            or _delivery_target_for_binding(candidate)
        )
        if target:
            return target
    return None


def _thread_target_for_outbound(binding: BotBinding, reply_in_thread: bool | None = None) -> tuple[BotReplyTarget | None, bool]:
    active_target = _active_reply_target_for_binding(binding)
    if active_target:
        return active_target, True if reply_in_thread is None else reply_in_thread

    own_target = _reply_target_for_binding(binding)
    if own_target:
        should_thread = _should_reply_in_external_thread(binding) if reply_in_thread is None else reply_in_thread
        return own_target, should_thread

    master_target = _master_reply_target_for_binding(binding)
    if master_target:
        return master_target, True if reply_in_thread is None else reply_in_thread

    delivery_target = _delivery_target_for_binding(binding)
    if delivery_target:
        should_thread = _should_reply_in_external_thread(binding) if reply_in_thread is None else reply_in_thread
        if should_thread:
            return delivery_target, True

    return None, False if reply_in_thread is None else reply_in_thread


def _outbound_bindings_for_thread(thread_id: str, bindings: list[BotBinding]) -> list[BotBinding]:
    targets = _load_bot_reply_targets()

    def score(binding: BotBinding) -> tuple[int, float, int, float]:
        active_target = _active_reply_target_for_thread_provider(binding.thread_id, binding.provider)
        if active_target:
            target = _active_reply_target_for_binding(binding)
            return (
                2 if target else 0,
                target.updated_at if target else 0,
                0,
                binding.updated_at,
            )
        target = targets.get(_reply_target_key(binding))
        target_score = target.updated_at if (target and _should_reply_in_external_thread(binding)) else 0
        return (
            1 if target_score else 0,
            target_score,
            1 if binding.is_primary_channel else 0,
            binding.updated_at,
        )

    selected: dict[str, BotBinding] = {}
    for binding in bindings:
        current = selected.get(binding.provider)
        if current is None or score(binding) > score(current):
            selected[binding.provider] = binding

    ordered: list[BotBinding] = list(selected.values())
    seen = {
        (binding.provider, binding.external_conversation_id, binding.thread_id)
        for binding in ordered
    }
    for binding in bindings:
        key = (binding.provider, binding.external_conversation_id, binding.thread_id)
        if key in seen:
            continue
        primary = selected.get(binding.provider)
        if primary is None:
            continue
        # Keep the best interactive binding per provider, but also mirror agent
        # updates into any additional passive report-channel bindings explicitly
        # attached to the same thread.
        if _active_reply_target_for_binding(binding) or _reply_target_for_binding(binding):
            continue
        if binding.post_in_thread:
            continue
        ordered.append(binding)
        seen.add(key)
    return ordered


def _remember_approval_message(
    request_id: int | str,
    *,
    connection_id: str,
    channel: str,
    message_ts: str,
    context: str,
    thread_id: str | None = None,
) -> None:
    messages = _load_approval_messages()
    key = str(request_id)
    current = messages.setdefault(key, [])
    if any(item.connection_id == connection_id and item.channel == channel and item.message_ts == message_ts for item in current):
        return
    current.append(
        ApprovalSlackMessage(
            request_id=key,
            connection_id=connection_id,
            channel=channel,
            message_ts=message_ts,
            context=context,
            thread_id=thread_id,
            created_at=time.time(),
        )
    )
    _save_approval_messages(messages)


def _forget_approval_messages(request_id: int | str) -> None:
    messages = _load_approval_messages()
    if messages.pop(str(request_id), None) is not None:
        _save_approval_messages(messages)


def _record_bot_detail(thread_id: str, item_type: str, title: str, text: str) -> None:
    if not text.strip():
        return
    details = _load_bot_details()
    items = details.setdefault(thread_id, [])
    items.append(
        BotThreadDetail(
            thread_id=thread_id,
            item_type=item_type,
            title=title,
            text=text,
            created_at=time.time(),
        )
    )
    details[thread_id] = items[-20:]
    _save_bot_details(details)


def _latest_bot_detail(thread_id: str) -> BotThreadDetail | None:
    items = _load_bot_details().get(thread_id) or []
    return items[-1] if items else None


def _upsert_indexed_thread(thread: IndexedThread) -> None:
    threads = _load_thread_index()
    for index, existing in enumerate(threads):
        if existing.id == thread.id:
            threads[index] = thread
            _save_thread_index(threads)
            return
    threads.append(thread)
    _save_thread_index(threads)


def _remove_indexed_thread(thread_id: str) -> None:
    threads = _load_thread_index()
    kept = [thread for thread in threads if thread.id != thread_id]
    if len(kept) != len(threads):
        _save_thread_index(kept)


def _save_json_private(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n", private=True)


def _work_item_handoff_timeout_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS") or "900")
    except ValueError:
        return 900.0
    return max(60.0, seconds)


def _default_thread_message_limit() -> int:
    try:
        limit = int(
            os.environ.get("CODEX_WEB_THREAD_MESSAGE_LIMIT")
            or os.environ.get("CODEX_WEB_THREAD_TURN_LIMIT")
            or DEFAULT_THREAD_MESSAGE_LIMIT
        )
    except ValueError:
        return DEFAULT_THREAD_MESSAGE_LIMIT
    return max(1, min(limit, 1000))


def _coerce_thread_message_limit(limit: int | None) -> int:
    if limit is None:
        return _default_thread_message_limit()
    return max(1, min(int(limit), 1000))


def _trim_thread_messages(response: dict[str, Any], limit: int) -> dict[str, Any]:
    if limit <= 0:
        return response
    thread = response.get("thread") if isinstance(response.get("thread"), dict) else response
    turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list):
        return response
    total_items = sum(len(turn.get("items") or []) for turn in turns if isinstance(turn, dict))
    if total_items <= limit:
        thread["messageLimit"] = limit
        return response
    remaining = limit
    kept_turns: list[dict[str, Any]] = []
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items") or []
        if not isinstance(items, list):
            items = []
        if remaining <= 0:
            break
        if len(items) <= remaining:
            kept_turns.append(turn)
            remaining -= len(items)
            continue
        kept_turn = {**turn, "items": items[-remaining:]}
        kept_turns.append(kept_turn)
        remaining = 0
    thread["turns"] = list(reversed(kept_turns))
    thread["messagesTruncated"] = True
    thread["messagesOmitted"] = total_items - limit
    thread["messageLimit"] = limit
    return response


def _work_item_progress_sla_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_WORK_ITEM_PROGRESS_SLA_SECONDS") or "3600")
    except ValueError:
        return 3600.0
    return max(300.0, seconds)


def _release_validation_sla_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_RELEASE_VALIDATION_SLA_SECONDS") or "1800")
    except ValueError:
        return 1800.0
    return max(300.0, seconds)


def _accepted_handoff_owner_idle_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_ACCEPTED_HANDOFF_OWNER_IDLE_SECONDS") or "300")
    except ValueError:
        return 300.0
    return max(60.0, seconds)


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


def _gitlab_event_target_agents(
    payload: dict[str, Any],
    project_settings: GitLabProjectRoutingSettings,
    projected_state: WorkItemState | None,
) -> list[str]:
    if projected_state:
        findings = _work_item_split_brain_findings(projected_state)
        if findings:
            return ["orchestrator"]
        if projected_state.handoff and projected_state.handoff.status == "pending":
            recipient = _coerce_owner(projected_state.handoff.to_agent)
            if recipient:
                return [recipient]
        if projected_state.current_stage == "failed_with_action_owner":
            owner = _coerce_owner(projected_state.current_owner or projected_state.next_owner)
            if owner:
                return [owner]
        owner = _coerce_owner(projected_state.current_owner)
        if owner and projected_state.current_stage in {
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }:
            return [owner]
    return _gitlab_routing_agents(payload, project_settings)


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


def _bot_connection(connection_id: str) -> BotConnection:
    for connection in _load_bot_connections():
        if connection.id == connection_id:
            return connection
    raise HTTPException(status_code=404, detail="Bot connection not found")


def _bot_connection_for_conversation(provider: str, external_conversation_id: str) -> BotConnection | None:
    for connection in _load_bot_connections():
        if connection.provider == provider and connection.default_external_conversation_id == external_conversation_id:
            return connection
    return None


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"


def _bot_connection_public(connection: BotConnection) -> dict[str, Any]:
    data = connection.model_dump()
    data["bot_token"] = _mask_secret(connection.bot_token)
    data["slack_app_token"] = _mask_secret(connection.slack_app_token)
    data["signing_secret"] = _mask_secret(connection.signing_secret)
    data["webhook_secret"] = _mask_secret(connection.webhook_secret)
    return data


def _connection_identity(connection: BotConnection | BotConnectionCreate) -> tuple[Any, ...]:
    return (
        connection.provider.lower(),
        connection.project_id,
        (connection.default_external_conversation_id or "").strip(),
        (connection.name or "").strip().lower(),
        connection.bot_token or "",
        connection.slack_app_token or "",
        connection.signing_secret or "",
        connection.webhook_secret or "",
    )


def _connection_matches_payload(connection: BotConnection, payload: BotConnectionCreate) -> bool:
    if payload.id and connection.id == payload.id:
        return True
    payload_identity = _connection_identity(
        BotConnection(
            id=connection.id,
            provider=payload.provider,
            name=payload.name,
            project_id=payload.project_id,
            bot_token=payload.bot_token or connection.bot_token,
            slack_app_token=payload.slack_app_token or connection.slack_app_token,
            signing_secret=payload.signing_secret or connection.signing_secret,
            webhook_secret=payload.webhook_secret or connection.webhook_secret,
            default_external_conversation_id=payload.default_external_conversation_id,
            default_external_name=payload.default_external_name,
            telegram_update_offset=connection.telegram_update_offset,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )
    )
    return _connection_identity(connection) == payload_identity


def _upsert_bot_connection(payload: BotConnectionCreate) -> BotConnection:
    now = time.time()
    provider = payload.provider.lower()
    if provider not in {"slack", "telegram"}:
        raise HTTPException(status_code=400, detail="Provider must be slack or telegram")
    _project(payload.project_id)
    connections = _load_bot_connections()
    for index, connection in enumerate(connections):
        if _connection_matches_payload(connection, payload):
            current = connection.model_dump()
            updates = payload.model_dump(exclude={"id"})
            for secret in ("bot_token", "slack_app_token", "signing_secret", "webhook_secret"):
                if updates.get(secret) in {None, "", "********"}:
                    updates[secret] = current.get(secret)
            current.update({key: value for key, value in updates.items() if value is not None})
            current["provider"] = provider
            current["updated_at"] = now
            updated = BotConnection.model_validate(current)
            connections[index] = updated
            _save_bot_connections(connections)
            _dedupe_bot_integrations()
            return updated

    connection = BotConnection(
        id=uuid.uuid4().hex[:12],
        provider=provider,
        name=payload.name,
        project_id=payload.project_id,
        bot_token=payload.bot_token or None,
        slack_app_token=payload.slack_app_token or None,
        signing_secret=payload.signing_secret or None,
        webhook_secret=payload.webhook_secret or None,
        default_external_conversation_id=payload.default_external_conversation_id or None,
        default_external_name=payload.default_external_name or None,
        created_at=now,
        updated_at=now,
    )
    connections.append(connection)
    _save_bot_connections(connections)
    _dedupe_bot_integrations()
    return connection


def _dedupe_bot_integrations() -> None:
    connections = sorted(_load_bot_connections(), key=lambda item: item.created_at)
    canonical_by_key: dict[tuple[Any, ...], BotConnection] = {}
    connection_rewrites: dict[str, str] = {}
    kept_connections: list[BotConnection] = []
    for connection in connections:
        key = _connection_identity(connection)
        canonical = canonical_by_key.get(key)
        if canonical:
            connection_rewrites[connection.id] = canonical.id
            continue
        canonical_by_key[key] = connection
        kept_connections.append(connection)

    bindings = sorted(_load_bot_bindings(), key=lambda item: item.created_at)
    seen_binding_routes: set[tuple[str, str, str, str]] = set()
    kept_bindings: list[BotBinding] = []
    for binding in bindings:
        if binding.connection_id in connection_rewrites:
            binding.connection_id = connection_rewrites[binding.connection_id]
        route_key = (
            binding.provider,
            binding.external_conversation_id,
            binding.thread_id,
            (_binding_prefix(binding) or "").lower(),
        )
        if route_key in seen_binding_routes:
            continue
        seen_binding_routes.add(route_key)
        kept_bindings.append(binding)

    if len(kept_connections) != len(connections):
        _save_bot_connections(kept_connections)
    if len(kept_bindings) != len(bindings) or connection_rewrites:
        _save_bot_bindings(kept_bindings)


def _update_bot_connection(connection_id: str, **updates: Any) -> None:
    connections = _load_bot_connections()
    changed = False
    for index, connection in enumerate(connections):
        if connection.id != connection_id:
            continue
        data = connection.model_dump()
        data.update(updates)
        data["updated_at"] = time.time()
        connections[index] = BotConnection.model_validate(data)
        changed = True
        break
    if changed:
        _save_bot_connections(connections)


def _remember_thread_run_settings(
    thread_id: str,
    *,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    developer_instructions: str | None = None,
) -> ThreadRunSettings:
    all_settings = _load_thread_settings()
    current = all_settings.get(thread_id, ThreadRunSettings())
    if sandbox is not None:
        current.sandbox = sandbox
    if approval_policy is not None:
        current.approval_policy = approval_policy
    if model is not None:
        current.model = model or None
    if reasoning_effort is not None:
        current.reasoning_effort = reasoning_effort or None
    if developer_instructions is not None:
        current.developer_instructions = _base_developer_instructions(thread_id, developer_instructions)
    all_settings[thread_id] = current
    _save_thread_settings(all_settings)
    _sync_bot_binding_settings(thread_id, current)
    return current


def _thread_run_settings(thread_id: str | None) -> ThreadRunSettings:
    if not thread_id:
        return ThreadRunSettings()
    settings = _load_thread_settings().get(thread_id)
    if settings:
        return settings
    bindings = _bindings_for_thread(thread_id)
    if bindings:
        return ThreadRunSettings(
            sandbox=bindings[0].sandbox,
            approval_policy=bindings[0].approval_policy,
        )
    return ThreadRunSettings()


def _codex_web_internal_base_url() -> str:
    override = (os.environ.get("CODEX_WEB_INTERNAL_BASE_URL") or "").strip()
    if override:
        return override.rstrip("/")
    port = int(os.environ.get("CODEX_WEB_PORT", "8765"))
    return f"http://127.0.0.1:{port}"


def _work_item_contract_binding(thread_id: str | None) -> BotBinding | None:
    if not thread_id:
        return None
    bindings = _bindings_for_thread(thread_id)
    return max(bindings, key=lambda item: item.updated_at) if bindings else None


def _gitlab_routing_enabled_for_project(project_id: str | None) -> bool:
    if not project_id:
        return False
    settings = _load_gitlab_routing_settings()
    if not settings.enabled:
        return False
    project_settings = settings.projects.get(project_id)
    return bool(project_settings and project_settings.enabled)


def _work_item_contract_instructions(thread_id: str | None) -> str | None:
    binding = _work_item_contract_binding(thread_id)
    if not binding or not _gitlab_routing_enabled_for_project(binding.project_id):
        return None
    role = (_binding_report_name(binding) or _binding_prefix(binding) or "Agent").strip()
    role_key = role.lower()
    base_url = _codex_web_internal_base_url()
    lines = [
        "codex-web structured work-item contract. These rules are mandatory for GitLab-driven work.",
        f"Use `{base_url}/api/work-items` as the system of record for ownership, handoff, and progress.",
        "Before calling a work-item endpoint, URL-encode the full GitLab ref path with `urllib.parse.quote(ref, safe='')`.",
        "Do not rely on Slack narration alone. Every meaningful GitLab work step must also update codex-web state.",
        "",
        "Required endpoint usage:",
        "- POST `/api/work-items/{ref}/progress` after every meaningful step, blocker change, owner change, or next-action change.",
        "- POST `/api/work-items/{ref}/handoff` immediately when you push work to another named agent.",
        "- POST `/api/work-items/{ref}/ack` immediately when you accept or reject a handoff addressed to you.",
        "",
        "Progress payload minimums:",
        f"- `actor`: `{role}`",
        "- `current_owner`: the agent currently responsible",
        "- `current_stage`: one of `implementation_active`, `ready_for_validation`, `validation_running`, `failed_with_action_owner`, `ready_to_close`, `closed`",
        "- `next_action`: one exact next action",
        "- `next_owner`: set this whenever the next owner differs from the current owner",
        "- `blocker`: one exact blocker if work is blocked, otherwise omit or clear it",
        "- `blocking_findings`: optional list of additional concrete defects or follow-up findings that support the single canonical blocker",
        "",
        "Handoff rules:",
        "- A handoff is not complete until the sender records `/handoff` and the recipient records `/ack`.",
        "- If you hand work to release/validation, update the stage accordingly and set the exact expected action.",
        "- If you receive a handoff, acknowledge it in the same turn before doing deeper work.",
        "",
        "Loop discipline:",
        "- Never stop at a status summary. Either keep working, hand off explicitly, or record one exact blocker with the next owner.",
        "- If GitLab labels or status changed, reconcile the work-item state in codex-web before ending the turn.",
    ]
    if binding.is_master or role_key in {"orchestrator", "codex"}:
        lines.extend(
            [
                "",
                "Orchestrator-specific rules:",
                "- For every open GitLab work item you touch, ensure there is always a current owner, an exact next action, and a follow-up path until the item is closed.",
                "- When an owner stalls, issue a direct follow-up to the named agent thread and record the reassignment or escalation through `/progress` or `/handoff` in the same turn.",
                "- If a handoff expires or validation stalls, do not just restate the blocker. Push the next owner and update the structured state so the watchdog loop can continue.",
            ]
        )
    return "\n".join(lines)


def _effective_developer_instructions(thread_id: str | None, instructions: str | None) -> str | None:
    parts = [part.strip() for part in (instructions, _work_item_contract_instructions(thread_id)) if part and part.strip()]
    if not parts:
        return None
    return "\n\n".join(parts)


def _base_developer_instructions(thread_id: str | None, instructions: str | None) -> str | None:
    if not instructions or not instructions.strip():
        return None
    normalized = instructions.strip()
    contract = _work_item_contract_instructions(thread_id)
    if contract:
        while contract in normalized:
            normalized = normalized.replace(contract, "").strip()
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized or None


def _sync_bot_binding_settings(thread_id: str, settings: ThreadRunSettings) -> None:
    bindings = _load_bot_bindings()
    changed = False
    for binding in bindings:
        if binding.thread_id != thread_id:
            continue
        binding_changed = False
        if settings.sandbox is not None and binding.sandbox != settings.sandbox:
            binding.sandbox = settings.sandbox
            binding_changed = True
        if settings.approval_policy is not None and binding.approval_policy != settings.approval_policy:
            binding.approval_policy = settings.approval_policy
            binding_changed = True
        if binding_changed:
            binding.updated_at = time.time()
            changed = True
    if changed:
        _save_bot_bindings(bindings)


def _thread_queue(thread_id: str | None) -> list[QueuedTurn]:
    if not thread_id:
        return []
    return _load_turn_queues().get(thread_id, [])


def _thread_queue_depth(thread_id: str | None) -> int:
    return len(_thread_queue(thread_id))


def _max_thread_queue_depth() -> int:
    try:
        value = int(os.environ.get("CODEX_WEB_MAX_THREAD_QUEUE_DEPTH") or "12")
    except ValueError:
        return 12
    return max(1, min(value, 100))


def _steer_window_seconds() -> float:
    try:
        value = float(os.environ.get("CODEX_WEB_STEER_WINDOW_SECONDS") or "60")
    except ValueError:
        return 60.0
    return max(1.0, value)


def _max_steers_per_window() -> int:
    try:
        value = int(os.environ.get("CODEX_WEB_MAX_STEERS_PER_WINDOW") or "4")
    except ValueError:
        return 4
    return max(1, min(value, 50))


def _record_thread_steer(thread_id: str, *, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    window = _steer_window_seconds()
    recent = THREAD_STEER_TIMES.setdefault(thread_id, deque())
    while recent and timestamp - recent[0] >= window:
        recent.popleft()
    if len(recent) >= _max_steers_per_window():
        retry_after = max(1, int(window - (timestamp - recent[0])))
        raise HTTPException(
            status_code=429,
            detail={
                "code": "thread_steer_rate_limited",
                "threadId": thread_id,
                "retryAfterSeconds": retry_after,
            },
        )
    recent.append(timestamp)


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


def _normalize_agent_channel_mapping(raw_channels: dict[str, Any]) -> dict[str, list[str]]:
    channels: dict[str, list[str]] = {}
    for agent, values in raw_channels.items():
        normalized_agent = str(agent).strip().lower()
        if not normalized_agent:
            continue
        if isinstance(values, str):
            candidate_channels = [values]
        else:
            candidate_channels = list(values or [])
        normalized_channels = sorted(
            {
                str(channel).strip()
                for channel in candidate_channels
                if str(channel).strip()
            }
        )
        if normalized_channels:
            channels[normalized_agent] = normalized_channels
    return channels


def _normalize_agent_channel_presence_project_settings(
    settings: AgentChannelPresenceProjectSettings,
) -> AgentChannelPresenceProjectSettings:
    return AgentChannelPresenceProjectSettings(
        agent_channels=_normalize_agent_channel_mapping(settings.agent_channels),
    )


def _normalize_agent_channel_presence_settings(
    settings: AgentChannelPresenceSettings,
) -> AgentChannelPresenceSettings:
    projects: dict[str, AgentChannelPresenceProjectSettings] = {}
    for project_id, project_settings in settings.projects.items():
        normalized_project_id = str(project_id).strip()
        if not normalized_project_id:
            continue
        projects[normalized_project_id] = _normalize_agent_channel_presence_project_settings(project_settings)
    return AgentChannelPresenceSettings(projects=projects)


def _migrate_agent_channel_presence_settings(raw: Any) -> AgentChannelPresenceSettings:
    if not isinstance(raw, dict):
        return AgentChannelPresenceSettings()
    raw_projects = raw.get("projects")
    projects: dict[str, AgentChannelPresenceProjectSettings] = {}
    if isinstance(raw_projects, dict):
        for project_id, project_settings in raw_projects.items():
            if not isinstance(project_settings, dict):
                continue
            channels = project_settings.get("agent_channels")
            if not isinstance(channels, dict):
                continue
            normalized_project_id = str(project_id).strip()
            if not normalized_project_id:
                continue
            projects[normalized_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
    else:
        legacy_project_id = str(raw.get("default_project_id") or "home").strip() or "home"
        channels = raw.get("agent_channels")
        if isinstance(channels, dict):
            projects[legacy_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
    return AgentChannelPresenceSettings(projects=projects)


def _legacy_agent_channel_presence_from_gitlab_file() -> AgentChannelPresenceSettings:
    if not GITLAB_ROUTING_FILE.exists():
        return AgentChannelPresenceSettings()
    with contextlib.suppress(Exception):
        raw = json.loads(GITLAB_ROUTING_FILE.read_text())
        if isinstance(raw, dict) and "projects" in raw:
            projects: dict[str, AgentChannelPresenceProjectSettings] = {}
            for project_id, project_settings in (raw.get("projects") or {}).items():
                if not isinstance(project_settings, dict):
                    continue
                channels = project_settings.get("agent_channels")
                if not isinstance(channels, dict):
                    continue
                normalized_project_id = str(project_id).strip()
                if not normalized_project_id:
                    continue
                projects[normalized_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
            if projects:
                return AgentChannelPresenceSettings(projects=projects)
        return _migrate_agent_channel_presence_settings(raw)
    return AgentChannelPresenceSettings()


def _normalize_string_list(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_gitlab_project_settings(settings: GitLabProjectRoutingSettings) -> GitLabProjectRoutingSettings:
    channel_ids = _normalize_string_list(settings.channel_ids)
    project_paths = sorted({path.strip().lower() for path in settings.project_paths if path and path.strip()})
    route_agents = sorted({str(agent).strip().lower() for agent in settings.route_agents if str(agent).strip()})
    fallbacks: dict[str, list[str]] = {}
    for kind, agents in settings.fallback_agents_by_kind.items():
        normalized_kind = str(kind).strip().lower()
        normalized_agents = sorted({str(agent).strip().lower() for agent in agents if str(agent).strip()})
        if normalized_kind and normalized_agents:
            fallbacks[normalized_kind] = normalized_agents
    return GitLabProjectRoutingSettings(
        enabled=settings.enabled,
        channel_ids=channel_ids,
        route_agents=route_agents,
        project_paths=project_paths,
        fallback_agents_by_kind=fallbacks,
    )


def _normalize_gitlab_routing_settings(settings: GitLabRoutingSettings) -> GitLabRoutingSettings:
    ignored = sorted({kind.strip().lower() for kind in settings.ignored_event_kinds if kind and kind.strip()})
    projects: dict[str, GitLabProjectRoutingSettings] = {}
    for project_id, project_settings in settings.projects.items():
        normalized_project_id = str(project_id).strip()
        if not normalized_project_id:
            continue
        projects[normalized_project_id] = _normalize_gitlab_project_settings(project_settings)
    return GitLabRoutingSettings(
        enabled=settings.enabled,
        ignored_event_kinds=ignored or ["note", "wiki_page"],
        projects=projects,
    )


def _migrate_gitlab_routing_settings(raw: Any) -> GitLabRoutingSettings:
    if not isinstance(raw, dict):
        return GitLabRoutingSettings()
    if "projects" in raw:
        projects: dict[str, GitLabProjectRoutingSettings] = {}
        for project_id, project_settings in (raw.get("projects") or {}).items():
            if not isinstance(project_settings, dict):
                continue
            normalized_project_id = str(project_id).strip()
            if not normalized_project_id:
                continue
            migrated = dict(project_settings)
            legacy_channel_id = str(migrated.get("channel_id") or "").strip()
            if legacy_channel_id and not migrated.get("channel_ids"):
                migrated["channel_ids"] = [legacy_channel_id]
            projects[normalized_project_id] = GitLabProjectRoutingSettings.model_validate(migrated)
        return GitLabRoutingSettings(
            enabled=bool(raw.get("enabled", True)),
            ignored_event_kinds=raw.get("ignored_event_kinds") or ["note", "wiki_page"],
            projects=projects or GitLabRoutingSettings().projects,
        )
    project_settings_by_id: dict[str, GitLabProjectRoutingSettings] = {}
    for mapping in raw.get("project_mappings") or []:
        if not isinstance(mapping, dict):
            continue
        project_id = str(mapping.get("project_id") or "").strip()
        namespace = str(mapping.get("namespace") or "").strip().lower()
        if not project_id:
            continue
        project_settings = project_settings_by_id.setdefault(project_id, GitLabProjectRoutingSettings())
        if namespace:
            project_settings.project_paths.append(namespace)
    if not project_settings_by_id:
        legacy_project_id = str(raw.get("default_project_id") or "home").strip() or "home"
        project_settings_by_id[legacy_project_id] = GitLabProjectRoutingSettings()
    fallback_agents = raw.get("fallback_agents_by_kind")
    enabled = bool(raw.get("enabled", True))
    for project_settings in project_settings_by_id.values():
        project_settings.enabled = enabled
        channel_id = str(raw.get("channel_id") or "").strip()
        if channel_id:
            project_settings.channel_ids = [channel_id]
        if isinstance(fallback_agents, dict):
            project_settings.fallback_agents_by_kind = fallback_agents
    return GitLabRoutingSettings(
        enabled=enabled,
        ignored_event_kinds=raw.get("ignored_event_kinds") or ["note", "wiki_page"],
        projects=project_settings_by_id,
    )


def _parse_agent_channel_overrides() -> dict[str, list[str]]:
    raw = os.environ.get("CODEX_WEB_AGENT_CHANNELS", "").strip()
    if not raw:
        return {}
    with contextlib.suppress(Exception):
        payload = json.loads(raw)
        if isinstance(payload, dict):
            result: dict[str, list[str]] = {}
            for key, value in payload.items():
                normalized_key = str(key).lower()
                if isinstance(value, str) and value.strip():
                    result[normalized_key] = [value.strip()]
                elif isinstance(value, list):
                    channels = [str(channel).strip() for channel in value if str(channel).strip()]
                    if channels:
                        result[normalized_key] = channels
            return result
    result: dict[str, list[str]] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            result[key] = [value]
    return result


def _preferred_agent_conversation(agent: str, project_id: str) -> str | None:
    normalized = agent.lower()
    channels = _preferred_agent_conversations(normalized, project_id)
    return channels[0] if channels else None


def _preferred_agent_conversations(agent: str, project_id: str, allowed_channels: list[str] | None = None) -> list[str]:
    normalized = agent.lower()
    settings = _load_agent_channel_presence_settings()
    project_settings = settings.projects.get(project_id)
    allowed = set(_normalize_string_list(allowed_channels)) if allowed_channels else None
    if project_settings and normalized in project_settings.agent_channels:
        channels = _normalize_string_list(project_settings.agent_channels[normalized])
        if allowed is not None:
            channels = [channel for channel in channels if channel in allowed]
        if channels:
            return channels
    overrides = _parse_agent_channel_overrides()
    if normalized in overrides:
        channels = _normalize_string_list(overrides[normalized])
        if allowed is not None:
            channels = [channel for channel in channels if channel in allowed]
        if channels:
            return channels
    return []


def _clone_binding_to_known_channel(source: BotBinding, channel_id: str) -> BotBinding:
    channel = next(
        (
            item
            for item in _bot_channels(source.project_id)
            if item.get("provider") == source.provider and item.get("id") == channel_id
        ),
        None,
    )
    return _clone_binding_to_conversation(
        source,
        channel_id,
        external_name=(channel or {}).get("name") or (channel or {}).get("label") or channel_id,
    )


def _gitlab_routing_agents(payload: dict[str, Any], project_settings: GitLabProjectRoutingSettings) -> list[str]:
    explicit = _normalize_string_list(project_settings.route_agents)
    return explicit or _gitlab_owner_agents(payload, project_settings)


def _gitlab_routing_bindings_for_agent(
    agent: str,
    project_id: str,
    project_settings: GitLabProjectRoutingSettings,
) -> list[BotBinding]:
    binding = _binding_for_agent(agent, project_id)
    if not binding:
        return []
    route_channels = _normalize_string_list(project_settings.channel_ids)
    if not route_channels:
        return [binding]
    preferred_channels = _preferred_agent_conversations(agent, project_id, route_channels)
    if not preferred_channels:
        return []
    return [_clone_binding_to_known_channel(binding, channel_id) for channel_id in preferred_channels]


def _gitlab_routing_bindings_for_master(
    project_id: str,
    project_settings: GitLabProjectRoutingSettings,
) -> list[BotBinding]:
    master = _master_binding(project_id)
    if not master:
        return []
    route_channels = _normalize_string_list(project_settings.channel_ids)
    if not route_channels or master.provider != "slack":
        return [master]
    return [_clone_binding_to_known_channel(master, channel_id) for channel_id in route_channels]


def _binding_for_agent(
    agent: str,
    project_id: str,
    *,
    preferred_conversation_id: str | None = None,
) -> BotBinding | None:
    normalized = agent.strip().lower()
    if not normalized:
        return None
    candidates = sorted(
        [
            binding
            for binding in _load_bot_bindings()
            if binding.project_id == project_id
            and not binding.is_master
            and (_binding_prefix(binding) or "").strip().lower() == normalized
        ],
        key=lambda binding: binding.updated_at,
        reverse=True,
    )
    if not candidates:
        candidates = sorted(
            [
                binding
                for binding in _load_bot_bindings()
                if binding.project_id == project_id
                and not binding.is_master
                and normalized in {
                    (_binding_prefix(binding) or "").strip().lower().split(" ", 1)[0],
                    (binding.thread_name or "").strip().lower().split(" ", 1)[0],
                }
            ],
            key=lambda binding: binding.updated_at,
            reverse=True,
        )
    if not candidates:
        return None
    if preferred_conversation_id:
        preferred = [
            binding for binding in candidates if binding.external_conversation_id == preferred_conversation_id
        ]
        if preferred:
            return preferred[0]
        source = candidates[0]
        allowed_channels = [binding.external_conversation_id for binding in candidates]
        if preferred_conversation_id in _preferred_agent_conversations(
            normalized,
            project_id,
            allowed_channels=allowed_channels,
        ):
            return _clone_binding_to_conversation(source, preferred_conversation_id)
    preferred_conversation = _preferred_agent_conversation(normalized, project_id)
    if preferred_conversation:
        preferred = [
            binding for binding in candidates if binding.external_conversation_id == preferred_conversation
        ]
        if preferred:
            return preferred[0]
        source = candidates[0]
        return _clone_binding_to_conversation(source, preferred_conversation)
    return candidates[0]


def _logical_binding_name(binding: BotBinding) -> str:
    name = _binding_report_name(binding) or binding.thread_name or _binding_prefix(binding)
    return name.strip().lower()


def _same_logical_binding(candidate: BotBinding, source: BotBinding) -> bool:
    return (
        candidate.provider == source.provider
        and candidate.project_id == source.project_id
        and _logical_binding_name(candidate) == _logical_binding_name(source)
    )


def _logical_bindings_for_binding(source: BotBinding) -> list[BotBinding]:
    return sorted(
        [
            binding
            for binding in _load_bot_bindings()
            if binding.thread_id == source.thread_id or _same_logical_binding(binding, source)
        ],
        key=lambda binding: binding.updated_at,
        reverse=True,
    )


def _preferred_binding_for_replacement(source: BotBinding, bindings: list[BotBinding]) -> BotBinding:
    for binding in bindings:
        if binding.external_conversation_id == source.external_conversation_id:
            return binding
    return bindings[0] if bindings else source


def _retarget_bot_targets(old_thread_id: str, new_thread_id: str) -> None:
    def rewrite(targets: dict[str, BotReplyTarget]) -> dict[str, BotReplyTarget]:
        rewritten: dict[str, BotReplyTarget] = {}
        for key, target in targets.items():
            next_key = key
            if key == old_thread_id:
                next_key = new_thread_id
            elif key.endswith(f":{old_thread_id}") and ":external:" not in key:
                next_key = f"{key.rsplit(':', 1)[0]}:{new_thread_id}"
            if target.thread_id == old_thread_id:
                target = target.model_copy(update={"thread_id": new_thread_id, "updated_at": time.time()})
            rewritten[next_key] = target
        return rewritten

    _save_bot_reply_targets(rewrite(_load_bot_reply_targets()))
    _save_bot_delivery_targets(rewrite(_load_bot_delivery_targets()))


def _retarget_thread_settings(old_thread_id: str, new_thread_id: str) -> None:
    settings = _load_thread_settings()
    old_settings = settings.pop(old_thread_id, None)
    if old_settings and new_thread_id not in settings:
        settings[new_thread_id] = old_settings
    if old_settings:
        _save_thread_settings(settings)


def _retarget_active_turn(old_thread_id: str, new_thread_id: str) -> None:
    active_turns = _load_active_turns()
    if active_turns.pop(old_thread_id, None) is not None:
        # A replacement is a new Codex session. The old turn may still emit a
        # completion event under the old id, so migrating its marker creates a
        # permanently busy replacement thread.
        _save_active_turns(active_turns)


def _retarget_turn_queue(old_thread_id: str, new_thread_id: str) -> None:
    queues = _load_turn_queues()
    queued = queues.pop(old_thread_id, [])
    if not queued:
        return
    for item in queued:
        item.thread_id = new_thread_id
        if item.reply_target and item.reply_target.thread_id == old_thread_id:
            item.reply_target = item.reply_target.model_copy(update={"thread_id": new_thread_id})
    queues.setdefault(new_thread_id, []).extend(queued)
    _save_turn_queues(queues)


def _retarget_bot_details(old_thread_id: str, new_thread_id: str) -> None:
    details = _load_bot_details()
    old_items = details.pop(old_thread_id, [])
    if not old_items:
        return
    details.setdefault(new_thread_id, [])
    details[new_thread_id] = (details[new_thread_id] + old_items)[-20:]
    _save_bot_details(details)


def _retarget_slack_thread_icon(old_thread_id: str, new_thread_id: str) -> None:
    icons = _load_slack_thread_icons()
    icon = icons.pop(old_thread_id, None)
    if icon and new_thread_id not in icons:
        icons[new_thread_id] = icon
    if icon:
        _save_slack_thread_icons(icons)


def _retarget_logical_bot_bindings(source: BotBinding, new_thread_id: str) -> BotBinding:
    bindings = _load_bot_bindings()
    now = time.time()
    changed: list[BotBinding] = []
    canonical_thread_name = source.thread_name or source.route_prefix or _binding_prefix(source)
    for binding in bindings:
        if binding.thread_id != source.thread_id and not _same_logical_binding(binding, source):
            continue
        binding.thread_id = new_thread_id
        if not binding.thread_name and canonical_thread_name:
            binding.thread_name = canonical_thread_name
        binding.sandbox = source.sandbox
        binding.approval_policy = source.approval_policy
        binding.updated_at = now
        changed.append(binding)
    if not changed:
        source.thread_id = new_thread_id
        source.updated_at = now
        changed.append(source)
        bindings.append(source)
    _save_bot_bindings(bindings)
    _dedupe_bot_integrations()
    return _preferred_binding_for_replacement(source, changed)


def _retarget_bot_thread_state(old_thread_id: str, new_thread_id: str) -> None:
    _retarget_bot_targets(old_thread_id, new_thread_id)
    _retarget_thread_settings(old_thread_id, new_thread_id)
    _retarget_active_turn(old_thread_id, new_thread_id)
    _retarget_turn_queue(old_thread_id, new_thread_id)
    _retarget_bot_details(old_thread_id, new_thread_id)
    _retarget_slack_thread_icon(old_thread_id, new_thread_id)


async def _archive_replaced_bot_thread(old_thread_id: str, new_thread_id: str) -> bool:
    _remove_indexed_thread(old_thread_id)
    try:
        await codex.request("thread/archive", {"threadId": old_thread_id})
    except Exception as exc:
        _append_bot_event(
            {
                "type": "stale_bot_thread_archive_failed",
                "old_thread_id": old_thread_id,
                "new_thread_id": new_thread_id,
                "error": _truncate_text(str(exc), 500),
            }
        )
        return False
    _append_bot_event(
        {
            "type": "stale_bot_thread_archived",
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
        }
    )
    return True


async def _replace_stale_bot_thread(binding: BotBinding, error: str) -> BotBinding:
    old_thread_id = binding.thread_id
    project = _project(binding.project_id)
    settings = _thread_run_settings(old_thread_id)
    sandbox = binding.sandbox or settings.sandbox or project.sandbox
    approval_policy = binding.approval_policy or settings.approval_policy or project.approval_policy
    thread_name = binding.thread_name or binding.route_prefix or _binding_prefix(binding)

    replacement_params = _project_params(
        project,
        {
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "sessionStartSource": "bot-thread-replacement",
        },
    )
    try:
        response = await codex.request("thread/start", replacement_params)
    except Exception as exc:
        text = str(exc).lower()
        if "unknown variant `bot-thread-replacement`" not in text and "sessionstartsource" not in text:
            raise
        replacement_params = _project_params(
            project,
            {
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "sessionStartSource": "startup",
            },
        )
        response = await codex.request("thread/start", replacement_params)
    new_thread_id = response["thread"]["id"]
    _remember_thread_run_settings(
        new_thread_id,
        sandbox=sandbox,
        approval_policy=approval_policy,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        developer_instructions=settings.developer_instructions,
    )
    if thread_name:
        with contextlib.suppress(Exception):
            await _set_thread_name(new_thread_id, thread_name)
        _upsert_indexed_thread(
            IndexedThread(id=new_thread_id, name=thread_name, cwd=project.path, path=project.path, updatedAt=time.time())
        )
    replacement = _retarget_logical_bot_bindings(
        binding.model_copy(update={"sandbox": sandbox, "approval_policy": approval_policy}),
        new_thread_id,
    )
    for prior_thread_id, replacement_thread_id in list(THREAD_REPLACEMENTS.items()):
        if replacement_thread_id == old_thread_id:
            THREAD_REPLACEMENTS[prior_thread_id] = new_thread_id
    THREAD_REPLACEMENTS[old_thread_id] = new_thread_id
    THREAD_TERMINAL_FAILURES.pop(old_thread_id, None)
    _retarget_bot_thread_state(old_thread_id, new_thread_id)
    archived_old_thread = await _archive_replaced_bot_thread(old_thread_id, new_thread_id)
    _append_bot_event(
        {
            "type": "stale_bot_thread_replaced",
            "provider": binding.provider,
            "project_id": binding.project_id,
            "logical_name": _logical_binding_name(binding),
            "old_thread_id": old_thread_id,
            "new_thread_id": new_thread_id,
            "archived_old_thread": archived_old_thread,
            "error": _truncate_text(error, 500),
        }
    )
    await hub.publish(
        {
            "type": "bot.thread.replaced",
            "provider": binding.provider,
            "projectId": binding.project_id,
            "oldThreadId": old_thread_id,
            "newThreadId": new_thread_id,
            "name": thread_name,
        }
    )
    return replacement


async def _replace_stale_web_thread(thread_id: str, project: Project, error: str) -> str:
    """Replace an unusable browser-only thread while preserving its run settings."""
    settings = _thread_run_settings(thread_id)
    response = await codex.request(
        "thread/start",
        _project_params(
            project,
            {
                "sandbox": settings.sandbox or project.sandbox,
                "approvalPolicy": settings.approval_policy or project.approval_policy,
                "sessionStartSource": "startup",
            },
        ),
    )
    new_thread_id = response["thread"]["id"]
    _remember_thread_run_settings(
        new_thread_id,
        sandbox=settings.sandbox or project.sandbox,
        approval_policy=settings.approval_policy or project.approval_policy,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        developer_instructions=settings.developer_instructions,
    )
    indexed = next((item for item in _load_thread_index() if item.id == thread_id), None)
    thread_name = indexed.name if indexed else None
    if thread_name:
        with contextlib.suppress(Exception):
            await _set_thread_name(new_thread_id, thread_name)
        _upsert_indexed_thread(
            IndexedThread(
                id=new_thread_id,
                name=thread_name,
                cwd=project.path,
                path=project.path,
                updatedAt=time.time(),
            )
        )
    for prior_thread_id, replacement_thread_id in list(THREAD_REPLACEMENTS.items()):
        if replacement_thread_id == thread_id:
            THREAD_REPLACEMENTS[prior_thread_id] = new_thread_id
    THREAD_REPLACEMENTS[thread_id] = new_thread_id
    THREAD_TERMINAL_FAILURES.pop(thread_id, None)
    _retarget_bot_thread_state(thread_id, new_thread_id)
    archived_old_thread = await _archive_replaced_bot_thread(thread_id, new_thread_id)
    event = {
        "type": "stale_web_thread_replaced",
        "project_id": project.id,
        "old_thread_id": thread_id,
        "new_thread_id": new_thread_id,
        "archived_old_thread": archived_old_thread,
        "error": _truncate_text(error, 500),
    }
    _append_bot_event(event)
    await hub.publish(
        {
            "type": "bot.thread.replaced",
            "projectId": project.id,
            "oldThreadId": thread_id,
            "newThreadId": new_thread_id,
            "name": thread_name,
        }
    )
    return new_thread_id


def _replacement_thread_id(thread_id: str) -> str | None:
    replacement = THREAD_REPLACEMENTS.get(thread_id)
    seen = {thread_id}
    while replacement and replacement not in seen:
        seen.add(replacement)
        next_replacement = THREAD_REPLACEMENTS.get(replacement)
        if not next_replacement:
            return replacement
        replacement = next_replacement
    return replacement


def _raise_if_thread_replaced(thread_id: str) -> None:
    replacement = _replacement_thread_id(thread_id)
    if not replacement:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "thread_replaced",
            "staleThreadReplaced": True,
            "oldThreadId": thread_id,
            "newThreadId": replacement,
            "threadId": replacement,
        },
    )


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


def _master_binding(project_id: str) -> BotBinding | None:
    masters = [binding for binding in _load_bot_bindings() if binding.project_id == project_id and binding.is_master]
    if not masters:
        return None
    return max(masters, key=lambda binding: binding.updated_at)


def _orchestrator_binding(project_id: str) -> BotBinding | None:
    masters = sorted(
        [binding for binding in _load_bot_bindings() if binding.project_id == project_id and binding.is_master],
        key=lambda binding: binding.updated_at,
        reverse=True,
    )
    for binding in masters:
        if (_binding_report_name(binding) or "").strip().lower() in {"orchestrator", "codex"}:
            return binding
    return masters[0] if masters else None


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


def _find_bot_binding(provider: str, external_conversation_id: str) -> BotBinding | None:
    bindings = _bindings_for_connection(provider, external_conversation_id)
    if len(bindings) == 1:
        return bindings[0]
    return None


def _first_binding_for_connection(provider: str, external_conversation_id: str | None) -> BotBinding | None:
    if not external_conversation_id:
        return None
    bindings = _bindings_for_connection(provider, external_conversation_id)
    return bindings[0] if bindings else None


def _bindings_for_connection(provider: str, external_conversation_id: str) -> list[BotBinding]:
    normalized_provider = provider.lower()
    return [
        binding
        for binding in _load_bot_bindings()
        if binding.provider == normalized_provider and binding.external_conversation_id == external_conversation_id
    ]


def _bindings_for_thread(thread_id: str) -> list[BotBinding]:
    return [binding for binding in _load_bot_bindings() if binding.thread_id == thread_id]


def _bindings_for_project(provider: str, project_id: str) -> list[BotBinding]:
    normalized_provider = provider.lower()
    return [
        binding
        for binding in _load_bot_bindings()
        if binding.provider == normalized_provider and binding.project_id == project_id
    ]


def _primary_binding_for_project(
    provider: str,
    project_id: str,
    external_conversation_id: str | None = None,
) -> BotBinding | None:
    masters = [binding for binding in _bindings_for_project(provider, project_id) if binding.is_master]
    if not masters:
        return None
    preferred_thread = next(
        (
            binding.thread_id
            for binding in masters
            if (_binding_prefix(binding) or "").strip().lower() in {"orchestrator", "codex"}
        ),
        None,
    )
    if not preferred_thread:
        thread_ids = {binding.thread_id for binding in masters}
        if len(thread_ids) == 1:
            preferred_thread = next(iter(thread_ids))
    if not preferred_thread:
        preferred_thread = max(masters, key=lambda binding: binding.updated_at).thread_id
    same_channel = [
        binding
        for binding in _bindings_for_project(provider, project_id)
        if binding.thread_id == preferred_thread and binding.external_conversation_id == external_conversation_id
    ]
    if same_channel:
        return same_channel[0]
    for binding in masters:
        if binding.thread_id == preferred_thread:
            return binding
    return None


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


def _forget_bot_reply_target(thread_id: str) -> None:
    for loader, saver in (
        (_load_bot_reply_targets, _save_bot_reply_targets),
        (_load_bot_delivery_targets, _save_bot_delivery_targets),
    ):
        targets = loader()
        removed = False
        for key, target in list(targets.items()):
            if key == thread_id or target.thread_id == thread_id:
                targets.pop(key, None)
                removed = True
        if removed:
            saver(targets)


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


def _slack_backfill_interval_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS") or "15")
    except ValueError:
        return 15.0
    return max(5.0, seconds)


def _slack_backfill_window_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_WINDOW_SECONDS") or "300")
    except ValueError:
        return 300.0
    return max(30.0, min(seconds, 3600.0))


def _slack_backfill_rate_limit_min_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MIN_SECONDS") or "60")
    except ValueError:
        return 60.0
    return max(5.0, seconds)


def _slack_backfill_rate_limit_max_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MAX_SECONDS") or "900")
    except ValueError:
        return 900.0
    return max(_slack_backfill_rate_limit_min_seconds(), seconds)


def _slack_backfill_cooldown_remaining_seconds() -> float:
    return max(0.0, SLACK_BACKFILL_COOLDOWN_UNTIL - time.time())


def _slack_backfill_retry_after(headers: Any) -> float | None:
    try:
        value = headers.get("Retry-After")
    except Exception:
        value = None
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return max(0.0, float(value))
    return None


def _slack_backfill_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode() or "{}")
            payload["_http_status"] = response.status
            return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code == 429:
            return {
                "ok": False,
                "error": "ratelimited",
                "_http_status": exc.code,
                "_retry_after": _slack_backfill_retry_after(exc.headers),
                "_detail": detail,
            }
        return {
            "ok": False,
            "error": f"HTTP {exc.code}: {detail}",
            "_http_status": exc.code,
            "_detail": detail,
        }


def _slack_backfill_is_rate_limited(response: dict[str, Any]) -> bool:
    return response.get("_http_status") == 429 or response.get("error") == "ratelimited"


def _slack_backfill_apply_rate_limit(response: dict[str, Any], context: dict[str, Any]) -> None:
    global SLACK_BACKFILL_COOLDOWN_UNTIL, SLACK_BACKFILL_RATE_LIMIT_FAILURES
    SLACK_BACKFILL_RATE_LIMIT_FAILURES += 1
    minimum = _slack_backfill_rate_limit_min_seconds()
    maximum = _slack_backfill_rate_limit_max_seconds()
    retry_after = response.get("_retry_after")
    fallback = min(maximum, minimum * (2 ** min(SLACK_BACKFILL_RATE_LIMIT_FAILURES - 1, 4)))
    delay = max(minimum, float(retry_after) if retry_after is not None else fallback)
    delay = min(maximum, delay)
    SLACK_BACKFILL_COOLDOWN_UNTIL = max(SLACK_BACKFILL_COOLDOWN_UNTIL, time.time() + delay)
    _append_bot_event(
        {
            "type": "slack_backfill_cooldown_set",
            "provider": "slack",
            "delay_seconds": delay,
            "retry_after": retry_after,
            "failure_count": SLACK_BACKFILL_RATE_LIMIT_FAILURES,
            **context,
        }
    )


def _slack_backfill_record_failure(event_type: str, response: dict[str, Any], context: dict[str, Any]) -> bool:
    _append_bot_event(
        {
            "type": event_type,
            "provider": "slack",
            **context,
            "error": response.get("error") or str(response),
        }
    )
    if _slack_backfill_is_rate_limited(response):
        _slack_backfill_apply_rate_limit(response, context)
        return True
    return False


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


def _slack_backfill_channels() -> list[tuple[BotConnection, str]]:
    connections = {connection.id: connection for connection in _load_bot_connections() if connection.provider == "slack" and connection.bot_token}
    pairs: dict[tuple[str, str], tuple[BotConnection, str]] = {}
    for binding in _load_bot_bindings():
        connection = connections.get(binding.connection_id or "")
        if connection and binding.external_conversation_id:
            pairs[(connection.id, binding.external_conversation_id)] = (connection, binding.external_conversation_id)
    for connection in connections.values():
        if connection.default_external_conversation_id:
            pairs.setdefault((connection.id, connection.default_external_conversation_id), (connection, connection.default_external_conversation_id))
    return list(pairs.values())


def _slack_backfill_thread_targets() -> list[tuple[BotConnection, str, str]]:
    connections = {connection.id: connection for connection in _load_bot_connections() if connection.provider == "slack" and connection.bot_token}
    bindings = {
        (binding.provider, binding.thread_id, binding.external_conversation_id): binding
        for binding in _load_bot_bindings()
        if binding.provider == "slack" and binding.connection_id
    }
    targets: list[BotReplyTarget] = []
    targets.extend(_load_bot_reply_targets().values())
    targets.extend(_load_bot_delivery_targets().values())
    targets.extend(active.reply_target for active in _load_active_turns().values() if active.reply_target)
    pairs: dict[tuple[str, str, str], tuple[BotConnection, str, str]] = {}
    for target in targets:
        if target.provider != "slack" or not target.external_conversation_id:
            continue
        thread_ts = target.external_thread_id or target.message_id
        if not thread_ts:
            continue
        binding = bindings.get((target.provider, target.thread_id, target.external_conversation_id))
        if not binding:
            candidates = [
                item
                for item in _load_bot_bindings()
                if item.provider == "slack"
                and item.thread_id == target.thread_id
                and item.external_conversation_id == target.external_conversation_id
                and item.connection_id
            ]
            binding = candidates[0] if candidates else None
        connection = connections.get(binding.connection_id or "") if binding else None
        if not connection:
            continue
        pairs[(connection.id, target.external_conversation_id, thread_ts)] = (
            connection,
            target.external_conversation_id,
            thread_ts,
        )
    return list(pairs.values())


async def _run_slack_backfill_cycle() -> None:
    global SLACK_BACKFILL_RATE_LIMIT_FAILURES
    recent_message_ids = _recent_inbound_message_ids()
    oldest = f"{max(0.0, time.time() - _slack_backfill_window_seconds()):.6f}"
    for connection, channel_id in _slack_backfill_channels():
        url = "https://slack.com/api/conversations.history?" + urllib.parse.urlencode(
            {"channel": channel_id, "oldest": oldest, "limit": "50"}
        )
        response = await asyncio.to_thread(_slack_backfill_get_json, url, {"Authorization": f"Bearer {connection.bot_token}"})
        if not response.get("ok"):
            rate_limited = _slack_backfill_record_failure(
                "slack_backfill_failed",
                response,
                {
                    "connection_id": connection.id,
                    "external_conversation_id": channel_id,
                },
            )
            if rate_limited:
                return
            continue
        SLACK_BACKFILL_RATE_LIMIT_FAILURES = 0
        for event in reversed(response.get("messages") or []):
            message_id = str(event.get("ts") or "").strip()
            if not message_id or message_id in SLACK_BACKFILL_SEEN or message_id in recent_message_ids:
                continue
            if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_deleted"}:
                SLACK_BACKFILL_SEEN.add(message_id)
                continue
            text = _strip_slack_mentions(event.get("text") or "")
            if not text:
                SLACK_BACKFILL_SEEN.add(message_id)
                continue
            SLACK_BACKFILL_SEEN.add(message_id)
            result = await _handle_bot_inbound(
                BotInboundMessage(
                    provider="slack",
                    external_conversation_id=channel_id,
                    connection_id=connection.id,
                    external_name=connection.default_external_name or channel_id,
                    sender_id=event.get("user"),
                    text=text,
                    project_id=connection.project_id,
                    external_thread_id=event.get("thread_ts") or message_id,
                    message_id=message_id,
                )
            )
            _append_bot_event(
                {
                    "type": "slack_backfill_dispatched",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "external_conversation_id": channel_id,
                    "message_id": message_id,
                    "thread_id": result.get("threadId"),
                    "queued": result.get("queued", False),
                    "ok": result.get("ok", False),
                }
            )
    for connection, channel_id, thread_ts in _slack_backfill_thread_targets():
        thread_key = (connection.id, channel_id, thread_ts)
        if thread_key in SLACK_BACKFILL_BAD_THREADS:
            continue
        url = "https://slack.com/api/conversations.replies?" + urllib.parse.urlencode(
            {"channel": channel_id, "ts": thread_ts, "oldest": oldest, "limit": "50"}
        )
        response = await asyncio.to_thread(_slack_backfill_get_json, url, {"Authorization": f"Bearer {connection.bot_token}"})
        if not response.get("ok"):
            if response.get("error") in {"thread_not_found", "channel_not_found", "not_in_channel"}:
                SLACK_BACKFILL_BAD_THREADS.add(thread_key)
            rate_limited = _slack_backfill_record_failure(
                "slack_thread_backfill_failed",
                response,
                {
                    "connection_id": connection.id,
                    "external_conversation_id": channel_id,
                    "external_thread_id": thread_ts,
                },
            )
            if rate_limited:
                return
            continue
        SLACK_BACKFILL_RATE_LIMIT_FAILURES = 0
        for event in reversed(response.get("messages") or []):
            message_id = str(event.get("ts") or "").strip()
            if not message_id or message_id == thread_ts or message_id in SLACK_BACKFILL_SEEN or message_id in recent_message_ids:
                continue
            if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_deleted"}:
                SLACK_BACKFILL_SEEN.add(message_id)
                continue
            text = _strip_slack_mentions(event.get("text") or "")
            if not text:
                SLACK_BACKFILL_SEEN.add(message_id)
                continue
            SLACK_BACKFILL_SEEN.add(message_id)
            result = await _handle_bot_inbound(
                BotInboundMessage(
                    provider="slack",
                    external_conversation_id=channel_id,
                    connection_id=connection.id,
                    external_name=connection.default_external_name or channel_id,
                    sender_id=event.get("user"),
                    text=text,
                    project_id=connection.project_id,
                    external_thread_id=event.get("thread_ts") or thread_ts,
                    message_id=message_id,
                )
            )
            _append_bot_event(
                {
                    "type": "slack_thread_backfill_dispatched",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "external_conversation_id": channel_id,
                    "external_thread_id": thread_ts,
                    "message_id": message_id,
                    "thread_id": result.get("threadId"),
                    "queued": result.get("queued", False),
                    "ok": result.get("ok", False),
                }
            )


async def _slack_backfill_loop() -> None:
    interval = _slack_backfill_interval_seconds()
    if interval <= 0:
        return
    while True:
        cooldown = _slack_backfill_cooldown_remaining_seconds()
        if cooldown > 0:
            await asyncio.sleep(max(interval, cooldown))
            continue
        try:
            await _run_slack_backfill_cycle()
        except Exception as exc:
            _append_bot_event({"type": "slack_backfill_loop_failed", "error": str(exc)})
        await asyncio.sleep(interval)


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

    slack_backfill_cooldown = _slack_backfill_cooldown_remaining_seconds()
    if SLACK_BACKFILL_RATE_LIMIT_FAILURES >= 2 and slack_backfill_cooldown > 0:
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


def _gitlab_event_id(request: Request, payload: dict[str, Any]) -> str:
    for header in ("x-gitlab-event-uuid", "x-request-id"):
        value = request.headers.get(header)
        if value:
            return value
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    parts = [
        str(payload.get("object_kind") or payload.get("event_name") or "gitlab"),
        str(project.get("id") or project.get("path_with_namespace") or ""),
        str(attrs.get("id") or attrs.get("iid") or attrs.get("sha") or attrs.get("commit_id") or ""),
        str(attrs.get("updated_at") or attrs.get("finished_at") or attrs.get("created_at") or ""),
        str(attrs.get("action") or attrs.get("state") or attrs.get("status") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _remember_gitlab_event(event_id: str) -> bool:
    now = time.time()
    for key, seen_at in list(GITLAB_EVENT_IDS.items()):
        if now - seen_at > 3600:
            GITLAB_EVENT_IDS.pop(key, None)
    if event_id in GITLAB_EVENT_IDS:
        return False
    GITLAB_EVENT_IDS[event_id] = now
    return True


def _support_servicedesk_project_paths() -> list[str]:
    raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS") or os.environ.get(
        "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"
    )
    values = raw.split(",") if raw else ["veridataops/support"]
    return [value.strip().lower().strip("/") for value in values if value.strip()]


def _support_servicedesk_owner_agent() -> str:
    return (os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_OWNER_AGENT") or "james").strip().lower() or "james"


def _support_servicedesk_project_matches(project_path: str) -> bool:
    normalized = project_path.strip().lower().strip("/")
    return bool(normalized and normalized in _support_servicedesk_project_paths())


def _support_servicedesk_ticket_key(payload: dict[str, Any]) -> str | None:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    project_id = project.get("id") or project.get("path_with_namespace")
    iid = attrs.get("iid")
    if project_id is None or iid is None:
        return None
    return f"{project_id}:{iid}"


def _is_support_servicedesk_ticket_payload(payload: dict[str, Any]) -> bool:
    kind = str(payload.get("object_kind") or payload.get("event_name") or "").lower()
    if kind != "issue":
        return False
    project_path = str((payload.get("project") or {}).get("path_with_namespace") or "")
    if not _support_servicedesk_project_matches(project_path):
        return False
    attrs = payload.get("object_attributes") or {}
    state = str(attrs.get("state") or payload.get("state") or "").lower()
    action = str(attrs.get("action") or "").lower()
    if action and action not in {"open", "reopen", "sweep"}:
        return False
    return bool(attrs.get("iid")) and state not in {"closed", "merged"}


def _remember_support_servicedesk_ticket(payload: dict[str, Any], source: str, event_id: str | None = None) -> bool:
    ticket_key = _support_servicedesk_ticket_key(payload)
    if not ticket_key:
        return False
    state = _load_support_servicedesk_state()
    tickets = state.setdefault("tickets", {})
    now = time.time()
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    if ticket_key in tickets:
        tickets[ticket_key]["last_seen_at"] = now
        tickets[ticket_key]["last_source"] = source
        _save_support_servicedesk_state(state)
        return False
    tickets[ticket_key] = {
        "first_seen_at": now,
        "last_seen_at": now,
        "first_source": source,
        "last_source": source,
        "event_id": event_id,
        "project": project.get("path_with_namespace") or project.get("id"),
        "iid": attrs.get("iid"),
        "title": attrs.get("title"),
        "url": attrs.get("url") or attrs.get("web_url"),
    }
    _save_support_servicedesk_state(state)
    return True


def _support_servicedesk_ticket_seen(payload: dict[str, Any]) -> bool:
    ticket_key = _support_servicedesk_ticket_key(payload)
    if not ticket_key:
        return False
    return ticket_key in _load_support_servicedesk_state().get("tickets", {})


def _format_support_servicedesk_prompt(payload: dict[str, Any], source: str, agent: str) -> str:
    attrs = payload.get("object_attributes") or {}
    labels = _gitlab_label_names(payload)
    url = _gitlab_url(payload)
    lines = [
        f"Support ServiceDesk ticket intake for {agent}: {_gitlab_reference(payload)}",
        f"Intake source: {source}",
    ]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    if url:
        lines.append(f"URL: {url}")
    if attrs.get("description"):
        lines.append("Ticket description is available in GitLab; inspect the linked ticket only as needed.")
    lines.extend(
        [
            "",
            "Handle Support intake triage for this new ticket. Do not change the ticket-response workflow.",
            "Do not poll generic queues. Do not use Slack tools, Slack connectors, MCP Slack apps, or direct Slack API calls.",
            "Keep any GitLab update concise and avoid repeating prior evidence.",
        ]
    )
    return "\n".join(lines)


async def _dispatch_support_servicedesk_ticket(
    payload: dict[str, Any],
    *,
    source: str,
    event_id: str | None = None,
    settings: GitLabRoutingSettings | None = None,
) -> dict[str, Any]:
    if not _is_support_servicedesk_ticket_payload(payload):
        return {"ok": True, "ignored": True, "reason": "not_support_servicedesk_ticket"}
    if _support_servicedesk_ticket_seen(payload):
        return {"ok": True, "ignored": True, "reason": "duplicate_support_ticket", "ticketKey": _support_servicedesk_ticket_key(payload)}

    project_id, project_settings = _gitlab_project_settings_for_payload(payload, settings)
    if not project_id or not project_settings or not project_settings.enabled:
        _append_bot_event(
            {
                "type": "support_servicedesk_ticket_ignored",
                "source": source,
                "event_id": event_id,
                "reason": "no_enabled_gitlab_project_route",
                "ticket_key": _support_servicedesk_ticket_key(payload),
            }
        )
        return {"ok": True, "accepted": False, "reason": "no_enabled_gitlab_project_route"}

    agent = next(iter(_gitlab_owner_agents(payload, project_settings)), _support_servicedesk_owner_agent())
    binding = _binding_for_agent(agent, project_id) or _master_binding(project_id)
    if not binding:
        _append_bot_event(
            {
                "type": "support_servicedesk_ticket_ignored",
                "source": source,
                "event_id": event_id,
                "reason": "no_matching_binding",
                "agent": agent,
                "ticket_key": _support_servicedesk_ticket_key(payload),
            }
        )
        return {"ok": False, "accepted": False, "reason": "no_matching_binding", "agent": agent}

    _remember_support_servicedesk_ticket(payload, source, event_id)
    result = await _dispatch_event_to_binding(
        binding,
        _format_support_servicedesk_prompt(payload, source, agent),
        source=f"gitlab:servicedesk:{source}",
    )
    target = {
        "agent": agent,
        "threadId": result.get("threadId"),
        "queued": result.get("queued", False),
        "ok": result.get("ok", False),
    }
    _append_bot_event(
        {
            "type": "support_servicedesk_ticket_dispatched",
            "source": source,
            "event_id": event_id,
            "project_id": project_id,
            "ticket_key": _support_servicedesk_ticket_key(payload),
            "target": target,
        }
    )
    await hub.publish(
        {
            "type": "support.servicedesk.ticket",
            "eventId": event_id,
            "projectId": project_id,
            "ticketKey": _support_servicedesk_ticket_key(payload),
            "target": target,
        }
    )
    return {"ok": True, "accepted": True, "eventId": event_id, "ticketKey": _support_servicedesk_ticket_key(payload), "targets": [target]}


def _gitlab_api_base_url() -> str:
    base = os.environ.get("CODEX_WEB_GITLAB_BASE_URL") or os.environ.get("GITLAB_BASE_URL") or "https://dev.veridataops.com/gitlab"
    return base.rstrip("/")


def _gitlab_api_token() -> str | None:
    return os.environ.get("CODEX_WEB_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")


def _support_servicedesk_sweep_project() -> str:
    return (
        os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_ID")
        or os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH")
        or "veridataops/support"
    ).strip()


def _support_servicedesk_sweep_interval() -> int:
    raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_SWEEP_INTERVAL_SECONDS", "3600")
    with contextlib.suppress(ValueError):
        return max(0, int(raw))
    return 3600


def _support_servicedesk_sweep_lookback_hours() -> int:
    raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_SWEEP_LOOKBACK_HOURS", "0")
    with contextlib.suppress(ValueError):
        return max(0, int(raw))
    return 0


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


def _issue_to_support_servicedesk_payload(issue: dict[str, Any], project_path: str, project_id: Any) -> dict[str, Any]:
    labels = issue.get("labels") or []
    return {
        "object_kind": "issue",
        "event_name": "issue",
        "project": {
            "id": project_id,
            "path_with_namespace": project_path,
            "web_url": issue.get("references", {}).get("full"),
        },
        "object_attributes": {
            "id": issue.get("id"),
            "iid": issue.get("iid"),
            "title": issue.get("title"),
            "description": issue.get("description"),
            "state": issue.get("state"),
            "action": "sweep",
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "url": issue.get("web_url"),
            "web_url": issue.get("web_url"),
        },
        "labels": [{"title": label} for label in labels if isinstance(label, str)],
    }


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


async def _run_support_servicedesk_sweep_once() -> dict[str, Any]:
    payloads = await asyncio.to_thread(_support_servicedesk_sweep_payloads)
    results: list[dict[str, Any]] = []
    settings = _load_gitlab_routing_settings()
    for payload in payloads:
        result = await _dispatch_support_servicedesk_ticket(payload, source="sweep", settings=settings)
        results.append(result)
    state = _load_support_servicedesk_state()
    state["last_sweep_at"] = time.time()
    _save_support_servicedesk_state(state)
    accepted = sum(1 for result in results if result.get("accepted"))
    duplicates = sum(1 for result in results if result.get("reason") == "duplicate_support_ticket")
    return {"ok": True, "checked": len(payloads), "accepted": accepted, "duplicates": duplicates, "results": results}


async def _support_servicedesk_sweep_loop() -> None:
    interval = _support_servicedesk_sweep_interval()
    if interval <= 0 or not _gitlab_api_token():
        return
    while True:
        try:
            result = await _run_support_servicedesk_sweep_once()
            _append_bot_event({"type": "support_servicedesk_sweep_completed", **{key: value for key, value in result.items() if key != "results"}})
        except Exception as exc:
            _append_bot_event({"type": "support_servicedesk_sweep_failed", "error": _truncate_text(str(exc), 500)})
        await asyncio.sleep(interval)


def _gitlab_semantic_dedupe_seconds() -> float:
    try:
        seconds = float(os.environ.get("CODEX_WEB_GITLAB_SEMANTIC_DEDUPE_SECONDS") or "300")
    except ValueError:
        return 300.0
    return max(30.0, seconds)


def _gitlab_semantic_key_for_state(
    ref: str | None,
    *,
    kind: str,
    labels: list[str],
    state: str | None,
) -> str | None:
    if not ref:
        return None
    payload = {
        "ref": ref,
        "kind": (kind or "issue").strip().lower(),
        "labels": sorted({label.strip() for label in labels if label and label.strip()}),
        "state": (state or "opened").strip().lower(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _gitlab_semantic_key(payload: dict[str, Any]) -> str | None:
    ref = _project_issue_ref(payload)
    if not ref:
        return None
    attrs = payload.get("object_attributes") or {}
    kind = str(payload.get("object_kind") or payload.get("event_name") or "issue")
    state = str(attrs.get("state") or attrs.get("status") or "opened")
    return _gitlab_semantic_key_for_state(
        ref,
        kind=kind,
        labels=_gitlab_label_names(payload),
        state=state,
    )


def _remember_gitlab_semantic_key(key: str | None, *, reason: str) -> bool:
    if not key:
        return True
    now = time.time()
    ttl = _gitlab_semantic_dedupe_seconds()
    events = {
        stored_key: seen_at
        for stored_key, seen_at in _load_gitlab_semantic_events().items()
        if now - seen_at <= max(ttl, 3600.0)
    }
    seen_at = events.get(key)
    if seen_at is not None and now - seen_at < ttl:
        _append_bot_event(
            {
                "type": "gitlab_semantic_duplicate_ignored",
                "reason": reason,
                "semantic_key": key,
                "age_seconds": now - seen_at,
            }
        )
        _save_gitlab_semantic_events(events)
        return False
    events[key] = now
    _save_gitlab_semantic_events(events)
    return True


def _remember_gitlab_semantic_issue_state(
    ref: str,
    *,
    labels: list[str],
    state: str | None,
    reason: str,
) -> None:
    key = _gitlab_semantic_key_for_state(ref, kind="issue", labels=labels, state=state)
    if not key:
        return
    events = _load_gitlab_semantic_events()
    events[key] = time.time()
    _save_gitlab_semantic_events(events)
    _append_bot_event({"type": "gitlab_semantic_state_recorded", "reason": reason, "ref": ref})


def _gitlab_label_names(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            labels.append(value)
        elif isinstance(value, dict):
            name = value.get("title") or value.get("name")
            if name:
                labels.append(str(name))

    attrs = payload.get("object_attributes") or {}
    for source in (
        payload.get("labels"),
        attrs.get("labels"),
        (payload.get("changes") or {}).get("labels", {}).get("current"),
    ):
        if isinstance(source, list):
            for item in source:
                add(item)
    for key in ("labels", "label_names"):
        source = attrs.get(key)
        if isinstance(source, list):
            for item in source:
                add(item)
    return sorted({label.strip() for label in labels if label and label.strip()})


def _gitlab_owner_agents(payload: dict[str, Any], project_settings: GitLabProjectRoutingSettings) -> list[str]:
    owners: list[str] = []
    for label in _gitlab_label_names(payload):
        match = re.match(r"owner::(.+)", label.strip(), re.IGNORECASE)
        if match:
            owners.append(match.group(1).strip().lower())
    if owners:
        return sorted(set(owners))
    kind = str(payload.get("object_kind") or payload.get("event_name") or "").lower()
    return project_settings.fallback_agents_by_kind.get(kind, [])


def _gitlab_project_path_matches(project_path: str, configured_path: str) -> bool:
    project_path = project_path.strip().lower().strip("/")
    configured_path = configured_path.strip().lower().strip("/")
    if not project_path or not configured_path:
        return False
    return project_path == configured_path or project_path.startswith(f"{configured_path}/")


def _gitlab_group_path(project_settings: GitLabProjectRoutingSettings) -> str | None:
    for path in project_settings.project_paths:
        normalized = (path or "").strip().strip("/")
        if not normalized:
            continue
        return normalized.split("/", 1)[0]
    return None


def _gitlab_token_for_project(project_id: str) -> str | None:
    env_token = (os.environ.get("CODEX_WEB_GITLAB_TOKEN") or "").strip()
    if env_token:
        return env_token
    with contextlib.suppress(Exception):
        secrets_path = Path(_project(project_id).path) / "CODEX-SECRETS.md"
        section = False
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
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


def _gitlab_project_settings_for_payload(
    payload: dict[str, Any],
    settings: GitLabRoutingSettings | None = None,
) -> tuple[str | None, GitLabProjectRoutingSettings | None]:
    project_path = ((payload.get("project") or {}).get("path_with_namespace") or "").lower()
    settings = settings or _load_gitlab_routing_settings()
    for project_id, project_settings in settings.projects.items():
        if any(_gitlab_project_path_matches(project_path, path) for path in project_settings.project_paths):
            return project_id, project_settings
    return None, None


def _gitlab_reference(payload: dict[str, Any]) -> str:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    kind = str(payload.get("object_kind") or payload.get("event_name") or "event").replace("_", " ")
    project_name = project.get("path_with_namespace") or project.get("name") or "unknown project"
    iid = attrs.get("iid")
    title = attrs.get("title") or attrs.get("name") or attrs.get("ref") or attrs.get("status") or ""
    if iid:
        return f"{project_name} {kind} !/#{iid}: {title}".strip()
    return f"{project_name} {kind}: {title}".strip()


def _gitlab_url(payload: dict[str, Any]) -> str | None:
    attrs = payload.get("object_attributes") or {}
    return attrs.get("url") or attrs.get("web_url") or (payload.get("project") or {}).get("web_url")


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


def _work_item_sla_threshold_seconds(state: WorkItemState) -> float:
    if (
        state.handoff
        and state.handoff.status == "accepted"
        and _coerce_owner(state.current_owner) == _coerce_owner(state.handoff.to_agent)
    ):
        if state.current_stage in {"ready_for_validation", "validation_running", "ready_to_close"}:
            return min(_release_validation_sla_seconds(), _accepted_handoff_owner_idle_seconds())
        return min(_work_item_progress_sla_seconds(), _accepted_handoff_owner_idle_seconds())
    if state.current_stage in {"ready_for_validation", "validation_running", "ready_to_close"}:
        return _release_validation_sla_seconds()
    return _work_item_progress_sla_seconds()


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








async def _work_item_sla_watchdog_loop() -> None:
    interval = _work_item_sla_watchdog_interval()
    if interval <= 0:
        return
    while True:
        try:
            await _run_work_item_sla_cycle()
        except Exception as exc:
            _append_bot_event({"type": "work_item_sla_watchdog_failed", "error": str(exc)})
        await asyncio.sleep(interval)


async def _orchestrator_watchdog_loop() -> None:
    interval = _orchestrator_watchdog_interval()
    if interval <= 0:
        return
    while True:
        try:
            await _run_orchestrator_watchdog_cycle()
        except Exception as exc:
            _append_bot_event({"type": "orchestrator_watchdog_failed", "error": str(exc)})
        await asyncio.sleep(interval)


async def _split_brain_watchdog_loop() -> None:
    interval = _split_brain_watchdog_interval()
    if interval <= 0:
        return
    while True:
        try:
            await _run_split_brain_watchdog_cycle()
        except Exception as exc:
            _append_bot_event({"type": "split_brain_watchdog_failed", "error": str(exc)})
        await asyncio.sleep(interval)


def _diagnostic_snapshot(project_id: str | None = None) -> dict[str, Any]:
    bindings = _load_bot_bindings()
    if project_id:
        _project(project_id)
        bindings = [binding for binding in bindings if binding.project_id == project_id]
    queues = _load_turn_queues()
    active_turns = _load_active_turns()
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
            "slackBackfillIntervalSeconds": _slack_backfill_interval_seconds(),
            "slackBackfillRunning": bool(SLACK_BACKFILL_TASK and not SLACK_BACKFILL_TASK.done()),
            "slackBackfillCooldownRemainingSeconds": _slack_backfill_cooldown_remaining_seconds(),
            "slackBackfillCooldownUntil": SLACK_BACKFILL_COOLDOWN_UNTIL or None,
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


async def _watchdog_loop() -> None:
    interval = _watchdog_interval()
    if interval <= 0:
        return
    while True:
        health = _daemon_health()
        if health["ok"]:
            _sd_notify("WATCHDOG=1\nSTATUS=codex-web healthy")
        else:
            # Service health is reported separately from process liveness. Keep
            # feeding systemd's watchdog while the API is responsive so an
            # operational warning (for example a stale queue) does not create a
            # destructive restart loop that makes recovery impossible.
            _sd_notify("WATCHDOG=1\nSTATUS=codex-web unhealthy: " + "; ".join(health["problems"]))
        await asyncio.sleep(interval)


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


async def _owner_work_watchdog_loop() -> None:
    interval = _owner_work_watchdog_interval()
    if interval <= 0:
        return
    while True:
        try:
            await _run_owner_work_watchdog_cycle()
        except Exception as exc:
            _append_bot_event({"type": "owner_work_watchdog_failed", "error": str(exc)})
        await asyncio.sleep(interval)


async def _release_gate_watchdog_loop() -> None:
    interval = _release_gate_watchdog_interval()
    if interval <= 0:
        return
    while True:
        try:
            await _run_release_gate_watchdog_cycle()
        except Exception as exc:
            _append_bot_event({"type": "release_gate_watchdog_failed", "error": str(exc)})
        await asyncio.sleep(interval)


async def _queue_recovery_loop() -> None:
    interval = _queue_recovery_interval_seconds()
    if interval <= 0:
        return
    while True:
        try:
            for thread_id in _load_turn_queues():
                if _thread_is_active(thread_id):
                    _release_stale_active_turn(thread_id, "queue-recovery")
                if not _thread_is_active(thread_id):
                    _schedule_queue_drain(thread_id)
        except Exception as exc:
            _append_bot_event({"type": "queue_recovery_failed", "error": str(exc)})
        await asyncio.sleep(interval)


@app.on_event("startup")
async def startup() -> None:
    global SUPPORT_SERVICEDESK_SWEEP_TASK, WATCHDOG_TASK, IS_SHUTTING_DOWN
    global WATCHDOG_TASK, OWNER_WORK_WATCHDOG_TASK, RELEASE_GATE_WATCHDOG_TASK, WORK_ITEM_SLA_TASK
    global ORCHESTRATOR_WATCHDOG_TASK, SPLIT_BRAIN_WATCHDOG_TASK, QUEUE_RECOVERY_TASK, SLACK_BACKFILL_TASK, IS_SHUTTING_DOWN
    IS_SHUTTING_DOWN = False
    _load_projects()
    _compact_turn_queues()
    _dedupe_bot_integrations()
    try:
        await codex.start()
    except Exception:
        # Keep the HTTP UI up so it can report the app-server failure.
        pass
    await bot_runtime.sync()
    if codex.ready.is_set() and _autonomy_enabled():
        asyncio.create_task(_restore_bot_thread_names())
        asyncio.create_task(_resume_active_threads_after_startup())
    _sd_notify("READY=1\nSTATUS=codex-web started")
    WATCHDOG_TASK = asyncio.create_task(_watchdog_loop())
    SUPPORT_SERVICEDESK_SWEEP_TASK = asyncio.create_task(_support_servicedesk_sweep_loop())
    OWNER_WORK_WATCHDOG_TASK = asyncio.create_task(_owner_work_watchdog_loop())
    RELEASE_GATE_WATCHDOG_TASK = asyncio.create_task(_release_gate_watchdog_loop())
    WORK_ITEM_SLA_TASK = asyncio.create_task(_work_item_sla_watchdog_loop())
    ORCHESTRATOR_WATCHDOG_TASK = asyncio.create_task(_orchestrator_watchdog_loop())
    SPLIT_BRAIN_WATCHDOG_TASK = asyncio.create_task(_split_brain_watchdog_loop())
    QUEUE_RECOVERY_TASK = asyncio.create_task(_queue_recovery_loop())
    SLACK_BACKFILL_TASK = asyncio.create_task(_slack_backfill_loop())
    _schedule_native_recovery_cycles()


@app.on_event("shutdown")
async def shutdown() -> None:
    global SUPPORT_SERVICEDESK_SWEEP_TASK, WATCHDOG_TASK, IS_SHUTTING_DOWN
    global WATCHDOG_TASK, OWNER_WORK_WATCHDOG_TASK, RELEASE_GATE_WATCHDOG_TASK, WORK_ITEM_SLA_TASK
    global ORCHESTRATOR_WATCHDOG_TASK, SPLIT_BRAIN_WATCHDOG_TASK, QUEUE_RECOVERY_TASK, SLACK_BACKFILL_TASK, IS_SHUTTING_DOWN
    IS_SHUTTING_DOWN = True
    _sd_notify("STOPPING=1\nSTATUS=codex-web stopping")
    if WATCHDOG_TASK:
        WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await WATCHDOG_TASK
        WATCHDOG_TASK = None
    if SUPPORT_SERVICEDESK_SWEEP_TASK:
        SUPPORT_SERVICEDESK_SWEEP_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SUPPORT_SERVICEDESK_SWEEP_TASK
        SUPPORT_SERVICEDESK_SWEEP_TASK = None
    if OWNER_WORK_WATCHDOG_TASK:
        OWNER_WORK_WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await OWNER_WORK_WATCHDOG_TASK
        OWNER_WORK_WATCHDOG_TASK = None
    if RELEASE_GATE_WATCHDOG_TASK:
        RELEASE_GATE_WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await RELEASE_GATE_WATCHDOG_TASK
        RELEASE_GATE_WATCHDOG_TASK = None
    if WORK_ITEM_SLA_TASK:
        WORK_ITEM_SLA_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await WORK_ITEM_SLA_TASK
        WORK_ITEM_SLA_TASK = None
    if ORCHESTRATOR_WATCHDOG_TASK:
        ORCHESTRATOR_WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ORCHESTRATOR_WATCHDOG_TASK
        ORCHESTRATOR_WATCHDOG_TASK = None
    if SPLIT_BRAIN_WATCHDOG_TASK:
        SPLIT_BRAIN_WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SPLIT_BRAIN_WATCHDOG_TASK
        SPLIT_BRAIN_WATCHDOG_TASK = None
    if QUEUE_RECOVERY_TASK:
        QUEUE_RECOVERY_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await QUEUE_RECOVERY_TASK
        QUEUE_RECOVERY_TASK = None
    if SLACK_BACKFILL_TASK:
        SLACK_BACKFILL_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await SLACK_BACKFILL_TASK
        SLACK_BACKFILL_TASK = None
    for task in list(ACTIONABLE_OWNER_CONTINUITY_TASKS.values()):
        task.cancel()
    ACTIONABLE_OWNER_CONTINUITY_TASKS.clear()
    for task in list(HANDOFF_CONTINUITY_TASKS.values()):
        task.cancel()
    HANDOFF_CONTINUITY_TASKS.clear()
    await bot_runtime.stop()
    await codex.stop()


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


@app.get("/api/status")
async def status() -> dict[str, Any]:
    try:
        await codex.ensure_started()
    except Exception:
        pass
    return {
        "ok": codex.ready.is_set(),
        "pid": codex.proc.pid if codex.proc else None,
        "error": None if codex.ready.is_set() else codex.last_error,
        "version": _static_version(),
        "pendingApprovals": list(codex.pending_approvals.values()),
        "activeTurns": len(_load_active_turns()),
        "queuedTurns": sum(len(items) for items in _load_turn_queues().values()),
    }


@app.get("/api/healthz")
async def healthz() -> dict[str, Any]:
    health = _daemon_health()
    if not health["ok"]:
        raise HTTPException(status_code=503, detail=health)
    return health


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


@app.get("/api/auth-verifier")
async def auth_verifier(request: Request) -> dict[str, Any]:
    expected = _codex_verifier_credentials()
    if not expected:
        raise HTTPException(status_code=404, detail="auth verifier disabled")
    provided = _basic_auth_credentials(request.headers.get("authorization"))
    if (
        not provided
        or not hmac.compare_digest(provided[0], expected[0])
        or not hmac.compare_digest(provided[1], expected[1])
    ):
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": 'Basic realm="VeridataOps codex-web verifier"'},
        )
    health = _daemon_health()
    return {
        "ok": health["ok"],
        "verified": True,
        "mode": "basic-auth-verifier",
        "version": _static_version(),
    }


@app.get("/api/diagnostics")
async def diagnostics(project_id: str | None = None) -> dict[str, Any]:
    return _diagnostic_snapshot(project_id)


@app.post("/api/diagnostics/route-test")
async def diagnostics_route_test(payload: BotRouteTest) -> dict[str, Any]:
    return _preview_bot_route(payload)


@app.get("/api/integrations/agent-presence")
async def get_agent_channel_presence() -> dict[str, Any]:
    return _agent_channel_presence_payload(_load_agent_channel_presence_settings())


def _agent_channel_presence_payload(settings: AgentChannelPresenceSettings) -> dict[str, Any]:
    return settings.model_dump()


@app.post("/api/integrations/agent-presence")
async def update_agent_channel_presence(payload: AgentChannelPresenceSettings) -> dict[str, Any]:
    settings = _save_agent_channel_presence_settings(payload)
    _append_bot_event({"type": "agent_channel_presence_updated", "settings": settings.model_dump()})
    await hub.publish({"type": "agent.channels.updated", "settings": _agent_channel_presence_payload(settings)})
    return {"ok": True, **_agent_channel_presence_payload(settings)}


@app.get("/api/integrations/gitlab")
async def get_gitlab_integration() -> dict[str, Any]:
    return _gitlab_integration_payload(_load_gitlab_routing_settings())


def _gitlab_integration_payload(settings: GitLabRoutingSettings) -> dict[str, Any]:
    return {
        **settings.model_dump(),
        "webhookPath": "/bots/gitlab/events",
        "tokenVerification": bool(
            os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
            or os.environ.get("GITLAB_WEBHOOK_SECRET")
        ),
    }


@app.post("/api/integrations/gitlab")
async def update_gitlab_integration(payload: GitLabRoutingSettings) -> dict[str, Any]:
    settings = _save_gitlab_routing_settings(payload)
    _append_bot_event({"type": "gitlab_routing_updated", "settings": settings.model_dump()})
    await hub.publish({"type": "gitlab.routing.updated", "settings": _gitlab_integration_payload(settings)})
    return {"ok": True, **_gitlab_integration_payload(settings)}


@app.post("/api/integrations/gitlab/support-servicedesk/sweep")
async def sweep_support_servicedesk() -> dict[str, Any]:
    if not _gitlab_api_token():
        raise HTTPException(status_code=503, detail="GitLab token is not configured for Support ServiceDesk sweep")
    try:
        return await _run_support_servicedesk_sweep_once()
    except Exception as exc:
        _append_bot_event({"type": "support_servicedesk_sweep_failed", "error": _truncate_text(str(exc), 500)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/work-items")
async def list_work_items(
    project_id: str | None = None,
    owner: str | None = None,
    stage: str | None = None,
    release_gate: bool | None = None,
) -> dict[str, Any]:
    states = list(_load_work_item_states().values())
    if project_id:
        states = [state for state in states if state.project_id == project_id]
    if owner:
        normalized_owner = _coerce_owner(owner)
        states = [state for state in states if _coerce_owner(state.current_owner or state.next_owner) == normalized_owner]
    if stage:
        normalized_stage = _normalize_work_item_stage(stage, fallback="")
        states = [state for state in states if state.current_stage == normalized_stage]
    if release_gate is not None:
        states = [state for state in states if state.release_gate is release_gate]
    states.sort(key=lambda item: item.updated_at, reverse=True)
    return {
        "items": [_work_item_state_public(state) for state in states],
        "count": len(states),
    }


@app.post("/api/work-items/sync-from-gitlab")
async def sync_work_items_from_gitlab() -> dict[str, Any]:
    global GITLAB_SYNC_CONSECUTIVE_FAILURES, GITLAB_SYNC_LAST_ERROR
    global GITLAB_SYNC_LAST_ERROR_AT, GITLAB_SYNC_LAST_SUCCESS_AT
    try:
        result = _sync_work_item_states_from_gitlab()
    except Exception as exc:
        GITLAB_SYNC_CONSECUTIVE_FAILURES += 1
        GITLAB_SYNC_LAST_ERROR = _truncate_text(str(exc), 500)
        GITLAB_SYNC_LAST_ERROR_AT = time.time()
        _append_bot_event(
            {
                "type": "gitlab_work_item_sync_failed",
                "failure_count": GITLAB_SYNC_CONSECUTIVE_FAILURES,
                "error": GITLAB_SYNC_LAST_ERROR,
            }
        )
        raise
    GITLAB_SYNC_CONSECUTIVE_FAILURES = 0
    GITLAB_SYNC_LAST_ERROR = None
    GITLAB_SYNC_LAST_SUCCESS_AT = time.time()
    await hub.publish({"type": "work-item.sync", **result})
    return {"ok": True, **result}


@app.get("/api/work-items/{ref:path}")
async def get_work_item(ref: str) -> dict[str, Any]:
    return _work_item_state_public(_work_item_state(ref))


@app.post("/api/work-items/{ref:path}/handoff")
async def create_work_item_handoff(ref: str, payload: WorkItemHandoffCreate) -> dict[str, Any]:
    state = _structured_handoff(ref, payload)
    await hub.publish({"type": "work-item.handoff", "ref": ref, "state": _work_item_state_public(state)})
    _schedule_structured_handoff_dispatch(state, source="work-item-handoff")
    _schedule_handoff_continuity_check(state, source="work-item-handoff-continuity")
    return {"ok": True, "item": _work_item_state_public(state)}


@app.post("/api/work-items/{ref:path}/ack")
async def ack_work_item_handoff(ref: str, payload: WorkItemAckCreate) -> dict[str, Any]:
    state = _structured_ack(ref, payload)
    await hub.publish({"type": "work-item.ack", "ref": ref, "state": _work_item_state_public(state)})
    _schedule_actionable_owner_dispatch(state, source="work-item-ack", actor=payload.actor)
    _schedule_actionable_owner_continuity_check(state, source="work-item-ack-continuity")
    if _work_item_split_brain_findings(state):
        _schedule_native_recovery_cycles(reason="work-item-ack-routing-drift")
    return {"ok": True, "item": _work_item_state_public(state)}


@app.post("/api/work-items/{ref:path}/progress")
async def update_work_item_progress(ref: str, payload: WorkItemProgressUpdate) -> dict[str, Any]:
    state = _structured_progress(ref, payload)
    await hub.publish({"type": "work-item.progress", "ref": ref, "state": _work_item_state_public(state)})
    _schedule_actionable_owner_dispatch(state, source="work-item-progress", actor=payload.actor)
    _schedule_actionable_owner_continuity_check(state, source="work-item-progress-continuity")
    if _work_item_split_brain_findings(state):
        _schedule_native_recovery_cycles(reason="work-item-progress-routing-drift")
    return {"ok": True, "item": _work_item_state_public(state)}


@app.post("/api/recovery/resume")
async def recovery_resume() -> dict[str, Any]:
    try:
        await codex.ensure_started()
    except Exception:
        pass
    now = time.time()
    stale_thread_ids = {
        thread_id
        for thread_id, active in _load_active_turns().items()
        if now - active.updated_at > _active_turn_stale_seconds()
    }
    if stale_thread_ids:
        asyncio.create_task(_resume_active_threads_after_startup(stale_thread_ids))
    for thread_id in _load_turn_queues():
        _schedule_queue_drain(thread_id)
    return {
        "ok": True,
        "resumingStaleThreads": sorted(stale_thread_ids),
        "activeTurns": len(_load_active_turns()),
        "queuedTurns": sum(len(items) for items in _load_turn_queues().values()),
    }


@app.get("/api/account/rate-limits")
async def account_rate_limits() -> dict[str, Any]:
    return await codex.request("account/rateLimits/read")


@app.get("/api/models")
async def list_models(include_hidden: bool = False) -> dict[str, Any]:
    try:
        return await codex.request("model/list", {"includeHidden": include_hidden, "limit": 100})
    except Exception as exc:
        _append_bot_event({"type": "model_list_failed", "error": str(exc)})
        return {"data": [], "nextCursor": None, "error": str(exc)}


@app.get("/api/bots")
async def bot_status() -> dict[str, Any]:
    bindings = _load_bot_bindings()
    connections = _load_bot_connections()
    gitlab_settings = _load_gitlab_routing_settings()
    gitlab_enabled = gitlab_settings.enabled and any(
        project.enabled and project.project_paths
        for project in gitlab_settings.projects.values()
    )
    return {
        "providers": {
            "slack": {
                "enabled": True,
                "signatureVerification": bool(
                    os.environ.get("SLACK_SIGNING_SECRET")
                    or any(connection.signing_secret for connection in connections if connection.provider == "slack")
                ),
                "eventsPath": "/bots/slack/events",
            },
            "telegram": {
                "enabled": True,
                "secretVerification": bool(
                    os.environ.get("TELEGRAM_WEBHOOK_SECRET")
                    or any(connection.webhook_secret for connection in connections if connection.provider == "telegram")
                ),
                "webhookPath": "/bots/telegram/webhook",
            },
            "gitlab": {
                "enabled": gitlab_enabled,
                "tokenVerification": bool(
                    os.environ.get("CODEX_WEB_GITLAB_WEBHOOK_SECRET")
                    or os.environ.get("GITLAB_WEBHOOK_SECRET")
                ),
                "webhookPath": "/bots/gitlab/events",
            },
        },
        "connections": len(connections),
        "bindings": len(bindings),
        "runtimeConnections": len(bot_runtime.tasks),
        "runtimeStatus": list(BOT_RUNTIME_STATUS.values()),
    }


@app.get("/api/bots/connections")
async def list_bot_connections() -> list[dict[str, Any]]:
    return [_bot_connection_public(connection) for connection in _load_bot_connections()]


@app.post("/api/bots/connections")
async def save_bot_connection(payload: BotConnectionCreate) -> dict[str, Any]:
    connection = _upsert_bot_connection(payload)
    await bot_runtime.sync()
    return _bot_connection_public(connection)


@app.get("/api/bots/bindings")
async def list_bot_bindings() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for binding in _load_bot_bindings():
        item = binding.model_dump()
        if binding.provider == "slack":
            item["slack_icon"] = _slack_reply_icon(binding)
            item["slack_username"] = _slack_reply_username(binding)
        results.append(item)
    return results


@app.get("/api/bots/channels")
async def list_bot_channels(project_id: str = "home") -> list[dict[str, str]]:
    _project(project_id)
    return _bot_channels(project_id)


@app.post("/api/bots/bindings")
async def create_bot_binding(payload: BotBindingCreate) -> dict[str, Any]:
    binding = await _start_bot_thread(payload)
    await bot_runtime.sync()
    return binding.model_dump()


@app.post("/api/bots/inbound")
async def bot_inbound(payload: BotInboundMessage) -> dict[str, Any]:
    return await _handle_bot_inbound(payload)


@app.post("/bots/gitlab/events")
@app.post("/bots/slack/events")
async def slack_events(request: Request) -> dict[str, Any]:
    body = await request.body()
    _verify_slack_signature(request, body)
    payload = json.loads(body or b"{}")

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    if payload.get("type") != "event_callback":
        return {"ok": True, "ignored": True}

    event = payload.get("event") or {}
    if event.get("type") not in {"message", "app_mention"}:
        return {"ok": True, "ignored": True}
    if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_deleted"}:
        return {"ok": True, "ignored": True}

    text = _strip_slack_mentions(event.get("text") or "")
    channel = event.get("channel")
    if not text or not channel:
        return {"ok": True, "ignored": True}

    connection = _bot_connection_for_conversation("slack", channel)
    message = BotInboundMessage(
        provider="slack",
        external_conversation_id=channel,
        connection_id=connection.id if connection else None,
        external_name=channel,
        sender_id=event.get("user"),
        text=text,
        external_thread_id=event.get("thread_ts") or event.get("ts"),
        message_id=event.get("ts"),
    )
    result = await _handle_bot_inbound(message)
    if result.get("ambiguous"):
        bindings = _bindings_for_connection("slack", channel)
        binding = bindings[0] if bindings else None
        connection = _bot_connection(binding.connection_id) if binding and binding.connection_id else None
        if connection and connection.bot_token:
            await asyncio.to_thread(
                _post_slack_message,
                connection.bot_token,
                channel,
                _ambiguous_route_message(result["availablePrefixes"]),
                username=_slack_reply_username(binding) if binding else None,
                icon_emoji=_slack_reply_icon(binding) if binding else None,
                thread_ts=event.get("thread_ts") or event.get("ts"),
            )
        return {"ok": True, "accepted": False, "ambiguous": True, "availablePrefixes": result["availablePrefixes"]}
    if result.get("timedOut"):
        return {"ok": True, "accepted": False, "timedOut": True, "threadId": result.get("threadId")}
    return {"ok": True, "accepted": True, "threadId": result["threadId"]}


@app.post("/bots/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, Any]:
    _verify_telegram_secret(request)
    payload = await request.json()
    message_payload = payload.get("message") or payload.get("edited_message") or {}
    text = (message_payload.get("text") or "").strip()
    chat = message_payload.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return {"ok": True, "ignored": True}

    sender = message_payload.get("from") or {}
    message = BotInboundMessage(
        provider="telegram",
        external_conversation_id=str(chat_id),
        external_name=chat.get("title") or chat.get("username") or str(chat_id),
        sender_id=str(sender.get("id")) if sender.get("id") is not None else None,
        sender_name=sender.get("username") or sender.get("first_name"),
        text=text,
        message_id=str(message_payload.get("message_id")) if message_payload.get("message_id") is not None else None,
    )
    result = await _handle_bot_inbound(message)
    if result.get("ambiguous"):
        return {"ok": True, "accepted": False, "ambiguous": True, "availablePrefixes": result["availablePrefixes"]}
    if result.get("timedOut"):
        return {"ok": True, "accepted": False, "timedOut": True, "threadId": result.get("threadId")}
    return {"ok": True, "accepted": True, "threadId": result["threadId"]}


@app.get("/api/projects")
async def list_projects() -> list[dict[str, Any]]:
    return [project.model_dump() for project in _load_projects()]


@app.post("/api/projects")
async def create_project(payload: ProjectCreate) -> dict[str, Any]:
    path = Path(payload.path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail="Project path must be an existing directory")
    projects = _load_projects()
    project_id = uuid.uuid4().hex[:12]
    project = Project(id=project_id, name=payload.name, path=str(path), model=payload.model, sandbox=payload.sandbox, approval_policy=payload.approval_policy)
    projects.append(project)
    _save_projects(projects)
    return project.model_dump()


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, bool]:
    projects = _load_projects()
    kept = [project for project in projects if project.id != project_id]
    if len(kept) == len(projects):
        raise HTTPException(status_code=404, detail="Project not found")
    if not kept:
        raise HTTPException(status_code=400, detail="At least one project is required")
    _save_projects(kept)
    return {"ok": True}


@app.get("/api/threads")
async def list_threads(project_id: str | None = None, archived: bool = False, search: str | None = None) -> dict[str, Any]:
    project_path = _project(project_id).path if project_id else None
    params: dict[str, Any] = {
        "limit": 100,
        "archived": archived,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "sourceKinds": ["appServer", "cli", "vscode", "exec"],
    }
    if project_path:
        params["cwd"] = project_path
    if search:
        params["searchTerm"] = search
    try:
        result = await codex.request("thread/list", params)
    except Exception as exc:
        if archived:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        result = {"data": []}
    if archived:
        return result

    items = result.get("data") or result.get("threads") or []
    indexed_threads = _load_thread_index()
    indexed_by_id = {indexed.id: indexed for indexed in indexed_threads}
    active_turns = _load_active_turns()
    for item in items:
        if not isinstance(item, dict):
            continue
        indexed = indexed_by_id.get(item.get("id"))
        if indexed:
            item["name"] = indexed.name
            item["updatedAt"] = max(
                item.get("updatedAt") or 0,
                indexed.updatedAt or 0,
                active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
            )
    existing_ids = {item.get("id") for item in items if isinstance(item, dict)}
    search_term = search.casefold() if search else None
    for indexed in indexed_threads:
        if indexed.id in existing_ids:
            continue
        if project_path and indexed.cwd and indexed.cwd != project_path:
            continue
        if search_term and search_term not in indexed.name.casefold():
            continue
        item: dict[str, Any] = {
            "id": indexed.id,
            "sessionId": indexed.id,
            "preview": "",
            "name": indexed.name,
            "cwd": indexed.cwd,
            "path": indexed.path,
            "updatedAt": max(indexed.updatedAt or 0, active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0),
            "status": {"type": "notLoaded"},
            "turns": [],
        }
        with contextlib.suppress(Exception):
            thread_response = await codex.request("thread/read", {"threadId": indexed.id, "includeTurns": False})
            thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
            item.update(thread)
            item["name"] = indexed.name
            item["updatedAt"] = max(
                item.get("updatedAt") or 0,
                indexed.updatedAt or 0,
                active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
            )
        items.append(item)
        existing_ids.add(indexed.id)

    items.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
    if "data" in result:
        result["data"] = items
    elif "threads" in result:
        result["threads"] = items
    else:
        result["data"] = items
    return result


@app.post("/api/threads")
async def create_thread(
    project_id: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    project = _project(project_id)
    response = await codex.request(
        "thread/start",
        _project_params(
            project,
            {
                "sessionStartSource": "startup",
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "model": model,
            },
        ),
    )
    thread = response.get("thread", response)
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if thread_id:
        _remember_thread_run_settings(
            thread_id,
            sandbox=sandbox or project.sandbox,
            approval_policy=approval_policy or project.approval_policy,
            model=model or project.model,
            reasoning_effort=reasoning_effort,
            developer_instructions=None,
        )
    return response


@app.get("/api/threads/{thread_id}")
async def read_thread(thread_id: str, message_limit: int | None = None, turn_limit: int | None = None) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    limit = _coerce_thread_message_limit(message_limit if message_limit is not None else turn_limit)
    resume_task = WEB_THREAD_RESUME_TASKS.get(thread_id)
    if resume_task and not resume_task.done():
        return _thread_read_timeout_response(
            thread_id,
            limit,
            "thread/resume still in progress",
            event_type="web_read_deferred_for_resume",
        )
    try:
        response = await codex.request("thread/read", {"threadId": thread_id, "includeTurns": True})
    except Exception as exc:
        if _is_codex_timeout_error(exc):
            return _thread_read_timeout_response(thread_id, limit, exc)
        raise
    return _trim_thread_messages(response, limit)


@app.post("/api/threads/{thread_id}/name")
async def rename_thread(thread_id: str, payload: ThreadRename) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    await _set_thread_name(thread_id, payload.name)
    return {"ok": True, "threadId": thread_id, "name": payload.name}


@app.post("/api/threads/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    project_id: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    force_resume: bool = False,
) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    project = _project(project_id)
    remembered = _thread_run_settings(thread_id)
    effective_sandbox = sandbox or remembered.sandbox or project.sandbox
    effective_approval_policy = approval_policy or remembered.approval_policy or project.approval_policy
    effective_model = model or remembered.model or project.model
    effective_reasoning_effort = reasoning_effort or remembered.reasoning_effort
    effective_developer_instructions = _effective_developer_instructions(thread_id, remembered.developer_instructions)
    _remember_thread_run_settings(
        thread_id,
        sandbox=effective_sandbox,
        approval_policy=effective_approval_policy,
        model=effective_model,
        reasoning_effort=effective_reasoning_effort,
        developer_instructions=remembered.developer_instructions,
    )
    params = {
        "threadId": thread_id,
        **_project_params(
            project,
            {
                "sandbox": effective_sandbox,
                "approvalPolicy": effective_approval_policy,
                "model": effective_model,
                "developerInstructions": effective_developer_instructions,
            },
        ),
    }
    if not force_resume:
        return {
            "ok": True,
            "threadId": thread_id,
            "skipped": True,
            "reason": "web_load_uses_thread_read",
        }
    task, scheduled = _web_thread_resume_task(thread_id, project.id, params)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_web_thread_resume_handoff_timeout())
    except asyncio.TimeoutError:
        if scheduled:
            _append_bot_event(
                {
                    "type": "web_resume_backgrounded",
                    "thread_id": thread_id,
                    "project_id": project.id,
                    "timeout_seconds": _web_thread_resume_handoff_timeout(),
                }
            )
        return {
            "ok": True,
            "resuming": True,
            "threadId": thread_id,
            "alreadyResuming": not scheduled,
        }


@app.post("/api/threads/{thread_id}/replace")
async def replace_bot_thread(thread_id: str) -> dict[str, Any]:
    existing_replacement = _replacement_thread_id(thread_id)
    if existing_replacement:
        return {
            "ok": True,
            "alreadyReplaced": True,
            "oldThreadId": thread_id,
            "newThreadId": existing_replacement,
            "queueDepth": _thread_queue_depth(existing_replacement),
        }
    bindings = _bindings_for_thread(thread_id)
    if not bindings:
        raise HTTPException(status_code=404, detail="No bot binding for this thread")
    replacement = await _replace_stale_bot_thread(bindings[0], "manual replacement requested")
    _schedule_queue_drain(replacement.thread_id)
    return {
        "ok": True,
        "oldThreadId": thread_id,
        "newThreadId": replacement.thread_id,
        "binding": _binding_public(replacement),
        "queueDepth": _thread_queue_depth(replacement.thread_id),
    }


@app.post("/api/threads/{thread_id}/turns")
async def start_turn(thread_id: str, payload: TurnCreate) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    project = _project(payload.project_id)
    remembered = _thread_run_settings(thread_id)
    effective_sandbox = payload.sandbox or remembered.sandbox or project.sandbox
    effective_approval_policy = payload.approval_policy or remembered.approval_policy or project.approval_policy
    effective_model = payload.model or remembered.model or project.model
    effective_reasoning_effort = payload.reasoning_effort or remembered.reasoning_effort
    effective_developer_instructions = _effective_developer_instructions(thread_id, remembered.developer_instructions)
    _remember_thread_run_settings(
        thread_id,
        sandbox=effective_sandbox,
        approval_policy=effective_approval_policy,
        model=effective_model,
        reasoning_effort=effective_reasoning_effort,
        developer_instructions=remembered.developer_instructions,
    )

    async def queue_web_turn(event_type: str, reason: str | None = None) -> dict[str, Any]:
        queued = _enqueue_turn(
            thread_id=thread_id,
            project_id=project.id,
            message=payload.message,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
        )
        queue_depth = _thread_queue_depth(thread_id)
        event_payload = {
            "type": event_type,
            "thread_id": thread_id,
            "project_id": project.id,
            "queued_id": queued.id,
            "queue_depth": queue_depth,
        }
        if reason:
            event_payload["reason"] = _truncate_text(reason, 500)
        _append_bot_event(event_payload)
        await _publish_queue_status(thread_id)
        if not _thread_is_active(thread_id):
            asyncio.get_running_loop().call_later(5, _schedule_queue_drain, thread_id)
        return {
            "queued": True,
            "queuedId": queued.id,
            "queueDepth": queue_depth,
            "threadId": thread_id,
        }

    _release_stale_active_turn(thread_id, "web:start")
    if _thread_is_active(thread_id) or _thread_queue_depth(thread_id):
        return await queue_web_turn("web_turn_queued")
    try:
        return await _start_thread_turn_now(
            thread_id,
            project=project,
            message=payload.message,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
        )
    except Exception as exc:
        if _is_codex_timeout_error(exc):
            result = await queue_web_turn("web_turn_queued_after_timeout", str(exc))
            result["timedOut"] = True
            result["error"] = str(getattr(exc, "detail", exc))
            return result
        if _is_stale_thread_error(exc):
            bindings = _bindings_for_thread(thread_id)
            if bindings:
                replacement = await _replace_stale_bot_thread(bindings[0], str(exc))
                new_thread_id = replacement.thread_id
            else:
                new_thread_id = await _replace_stale_web_thread(thread_id, project, str(exc))
            return {
                "ok": False,
                "staleThreadReplaced": True,
                "threadId": new_thread_id,
                "oldThreadId": thread_id,
                "newThreadId": new_thread_id,
            }
        raise


@app.get("/api/threads/{thread_id}/queue")
async def thread_queue(thread_id: str) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    return {
        "threadId": thread_id,
        "active": _thread_is_active(thread_id),
        "queueDepth": _thread_queue_depth(thread_id),
        "queued": [
            {
                "id": queued.id,
                "source": queued.source,
                "createdAt": queued.created_at,
                "attempts": queued.attempts,
            }
            for queued in _thread_queue(thread_id)
        ],
    }


@app.post("/api/threads/{thread_id}/queue/steer")
async def steer_queued_turn(thread_id: str) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    queued = _pop_latest_queued_turn(thread_id)
    if not queued:
        raise HTTPException(status_code=404, detail="No queued message for this thread")
    return await _steer_queued_turn(thread_id, queued)


@app.post("/api/threads/{thread_id}/queue/{queued_id}/steer")
async def steer_specific_queued_turn(thread_id: str, queued_id: str) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    queued = _pop_queued_turn(thread_id, queued_id)
    if not queued:
        raise HTTPException(status_code=404, detail="Queued message not found for this thread")
    return await _steer_queued_turn(thread_id, queued)


async def _steer_queued_turn(thread_id: str, queued: QueuedTurn) -> dict[str, Any]:
    if _thread_is_active(thread_id):
        try:
            _record_thread_steer(thread_id)
        except HTTPException:
            _requeue_turn_front(queued)
            raise
        with contextlib.suppress(Exception):
            await codex.request("turn/interrupt", {"threadId": thread_id})
        _clear_thread_active(thread_id)
    project = _project(queued.project_id)
    response = await _start_thread_turn_now(
        thread_id,
        project=project,
        message=queued.message,
        sandbox=queued.sandbox or project.sandbox,
        approval_policy=queued.approval_policy or project.approval_policy,
        model=queued.model,
        reasoning_effort=queued.reasoning_effort,
        source=f"steer:{queued.source}",
        reply_target=queued.reply_target,
    )
    return {
        "ok": True,
        "steeredId": queued.id,
        "queueDepth": _thread_queue_depth(thread_id),
        "turn": response.get("turn") if isinstance(response, dict) else None,
    }


@app.post("/api/threads/{thread_id}/settings")
async def update_thread_settings(thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    settings = _remember_thread_run_settings(
        thread_id,
        sandbox=payload.sandbox,
        approval_policy=payload.approval_policy,
        model=payload.model,
        reasoning_effort=payload.reasoning_effort,
        developer_instructions=payload.developer_instructions,
    )
    return {"ok": True, "threadId": thread_id, **settings.model_dump()}


@app.get("/api/thread-settings")
async def list_thread_settings() -> dict[str, Any]:
    return {thread_id: settings.model_dump() for thread_id, settings in _load_thread_settings().items()}


@app.get("/api/threads/{thread_id}/settings")
async def get_thread_settings(thread_id: str) -> dict[str, Any]:
    _raise_if_thread_replaced(thread_id)
    return {"threadId": thread_id, **_thread_run_settings(thread_id).model_dump()}


@app.post("/api/threads/{thread_id}/primary")
async def update_thread_primary(thread_id: str, payload: ThreadPrimaryUpdate) -> dict[str, Any]:
    bindings = await _set_thread_primary(thread_id, payload.project_id, payload.primary)
    return {
        "ok": True,
        "threadId": thread_id,
        "projectId": payload.project_id,
        "primary": payload.primary,
        "bindings": [binding.model_dump() for binding in bindings],
    }


@app.post("/api/threads/{thread_id}/primary-channel")
async def update_thread_primary_channel(thread_id: str, payload: ThreadPrimaryChannelUpdate) -> dict[str, Any]:
    bindings = await _set_thread_primary_channel(
        thread_id,
        payload.project_id,
        payload.provider,
        payload.external_conversation_id,
    )
    return {
        "ok": True,
        "threadId": thread_id,
        "projectId": payload.project_id,
        "provider": payload.provider,
        "externalConversationId": payload.external_conversation_id,
        "bindings": [binding.model_dump() for binding in bindings],
    }


@app.post("/api/threads/{thread_id}/archive")
async def archive_thread(thread_id: str) -> dict[str, Any]:
    return await codex.request("thread/archive", {"threadId": thread_id})


@app.post("/api/threads/{thread_id}/unarchive")
async def unarchive_thread(thread_id: str) -> dict[str, Any]:
    return await codex.request("thread/unarchive", {"threadId": thread_id})


@app.post("/api/turns/interrupt")
async def interrupt_turn(thread_id: str) -> dict[str, Any]:
    return await codex.request("turn/interrupt", {"threadId": thread_id})


@app.get("/api/approvals")
async def approvals() -> list[dict[str, Any]]:
    return list(codex.pending_approvals.values())


@app.post("/api/approvals/{request_id}")
async def decide_approval(request_id: str, payload: ApprovalDecision) -> dict[str, bool]:
    normalized_id = _request_id_value(request_id)
    return await _resolve_approval_request(normalized_id, payload.decision, actor="Codex Web")


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
