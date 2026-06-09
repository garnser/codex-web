from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from codex_web.events import EventHub
from codex_web.models import (
    ActiveThreadTurn,
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
    IndexedThread,
    Project,
    ProjectCreate,
    QueuedTurn,
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRename,
    ThreadRunSettings,
    TurnCreate,
)
from codex_web.paths import (
    ACTIVE_TURNS_FILE,
    APPROVAL_MESSAGES_FILE,
    BOT_DELIVERY_TARGETS_FILE,
    BOT_DETAILS_FILE,
    BOT_REPLY_TARGETS_FILE,
    BOTS_BINDINGS_FILE,
    BOTS_CONNECTIONS_FILE,
    BOTS_EVENTS_FILE,
    DATA_DIR,
    PROJECTS_FILE,
    SLACK_RELAY_NOTICE,
    SLACK_THREAD_ICONS_FILE,
    STATIC_DIR,
    THREAD_INDEX_FILE,
    THREAD_SETTINGS_FILE,
    TURN_QUEUE_FILE,
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
QUEUE_DRAIN_TASKS: dict[str, asyncio.Task[None]] = {}
IS_SHUTTING_DOWN = False


hub = EventHub()


def _load_projects() -> list[Project]:
    DATA_DIR.mkdir(exist_ok=True)
    if not PROJECTS_FILE.exists():
        defaults = [
            Project(id="home", name="Home", path=str(Path.home())),
        ]
        _save_projects(defaults)
        return defaults
    return [Project.model_validate(item) for item in json.loads(PROJECTS_FILE.read_text())]


def _save_projects(projects: list[Project]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PROJECTS_FILE.write_text(json.dumps([p.model_dump() for p in projects], indent=2) + "\n")


def _load_thread_index() -> list[IndexedThread]:
    DATA_DIR.mkdir(exist_ok=True)
    if not THREAD_INDEX_FILE.exists():
        return []
    return [IndexedThread.model_validate(item) for item in json.loads(THREAD_INDEX_FILE.read_text())]


def _save_thread_index(threads: list[IndexedThread]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    THREAD_INDEX_FILE.write_text(json.dumps([thread.model_dump() for thread in threads], indent=2) + "\n")


def _load_bot_reply_targets() -> dict[str, BotReplyTarget]:
    DATA_DIR.mkdir(exist_ok=True)
    if not BOT_REPLY_TARGETS_FILE.exists():
        return {}
    payload = json.loads(BOT_REPLY_TARGETS_FILE.read_text())
    return {thread_id: BotReplyTarget.model_validate(item) for thread_id, item in payload.items()}


def _save_bot_reply_targets(targets: dict[str, BotReplyTarget]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BOT_REPLY_TARGETS_FILE.write_text(
        json.dumps({thread_id: target.model_dump() for thread_id, target in targets.items()}, indent=2) + "\n"
    )


def _load_slack_thread_icons() -> dict[str, str]:
    DATA_DIR.mkdir(exist_ok=True)
    if not SLACK_THREAD_ICONS_FILE.exists():
        return {}
    return {str(key): str(value) for key, value in json.loads(SLACK_THREAD_ICONS_FILE.read_text()).items()}


def _save_slack_thread_icons(icons: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SLACK_THREAD_ICONS_FILE.write_text(json.dumps(icons, indent=2, sort_keys=True) + "\n")


def _load_bot_delivery_targets() -> dict[str, BotReplyTarget]:
    DATA_DIR.mkdir(exist_ok=True)
    if not BOT_DELIVERY_TARGETS_FILE.exists():
        return {}
    payload = json.loads(BOT_DELIVERY_TARGETS_FILE.read_text())
    return {thread_id: BotReplyTarget.model_validate(item) for thread_id, item in payload.items()}


def _save_bot_delivery_targets(targets: dict[str, BotReplyTarget]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BOT_DELIVERY_TARGETS_FILE.write_text(
        json.dumps({thread_id: target.model_dump() for thread_id, target in targets.items()}, indent=2) + "\n"
    )


def _reply_target_key(binding: BotBinding) -> str:
    return f"{binding.provider}:{binding.external_conversation_id}:{binding.thread_id}"


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
    return list(selected.values())


def _load_bot_details() -> dict[str, list[BotThreadDetail]]:
    DATA_DIR.mkdir(exist_ok=True)
    if not BOT_DETAILS_FILE.exists():
        return {}
    payload = json.loads(BOT_DETAILS_FILE.read_text())
    return {
        thread_id: [BotThreadDetail.model_validate(item) for item in items]
        for thread_id, items in payload.items()
    }


def _save_bot_details(details: dict[str, list[BotThreadDetail]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BOT_DETAILS_FILE.write_text(
        json.dumps(
            {thread_id: [item.model_dump() for item in items[-20:]] for thread_id, items in details.items()},
            indent=2,
        )
        + "\n"
    )


def _load_approval_messages() -> dict[str, list[ApprovalSlackMessage]]:
    DATA_DIR.mkdir(exist_ok=True)
    if not APPROVAL_MESSAGES_FILE.exists():
        return {}
    payload = json.loads(APPROVAL_MESSAGES_FILE.read_text())
    return {
        request_id: [ApprovalSlackMessage.model_validate(item) for item in items]
        for request_id, items in payload.items()
    }


def _save_approval_messages(messages: dict[str, list[ApprovalSlackMessage]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    APPROVAL_MESSAGES_FILE.write_text(
        json.dumps(
            {
                request_id: [message.model_dump() for message in items]
                for request_id, items in messages.items()
                if items
            },
            indent=2,
        )
        + "\n"
    )


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


def _save_json_private(path: Path, payload: Any) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_bot_connections() -> list[BotConnection]:
    DATA_DIR.mkdir(exist_ok=True)
    if not BOTS_CONNECTIONS_FILE.exists():
        return []
    return [BotConnection.model_validate(item) for item in json.loads(BOTS_CONNECTIONS_FILE.read_text())]


def _save_bot_connections(connections: list[BotConnection]) -> None:
    _save_json_private(BOTS_CONNECTIONS_FILE, [connection.model_dump() for connection in connections])


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


def _load_bot_bindings() -> list[BotBinding]:
    DATA_DIR.mkdir(exist_ok=True)
    if not BOTS_BINDINGS_FILE.exists():
        return []
    return [BotBinding.model_validate(item) for item in json.loads(BOTS_BINDINGS_FILE.read_text())]


def _save_bot_bindings(bindings: list[BotBinding]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BOTS_BINDINGS_FILE.write_text(json.dumps([binding.model_dump() for binding in bindings], indent=2) + "\n")


def _load_thread_settings() -> dict[str, ThreadRunSettings]:
    DATA_DIR.mkdir(exist_ok=True)
    if not THREAD_SETTINGS_FILE.exists():
        return {}
    return {
        thread_id: ThreadRunSettings.model_validate(settings)
        for thread_id, settings in json.loads(THREAD_SETTINGS_FILE.read_text()).items()
    }


def _save_thread_settings(settings: dict[str, ThreadRunSettings]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    THREAD_SETTINGS_FILE.write_text(
        json.dumps({thread_id: value.model_dump() for thread_id, value in settings.items()}, indent=2) + "\n"
    )


def _remember_thread_run_settings(
    thread_id: str,
    *,
    sandbox: str | None = None,
    approval_policy: str | None = None,
) -> ThreadRunSettings:
    all_settings = _load_thread_settings()
    current = all_settings.get(thread_id, ThreadRunSettings())
    if sandbox is not None:
        current.sandbox = sandbox
    if approval_policy is not None:
        current.approval_policy = approval_policy
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
        return ThreadRunSettings(sandbox=bindings[0].sandbox, approval_policy=bindings[0].approval_policy)
    return ThreadRunSettings()


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


def _load_active_turns() -> dict[str, ActiveThreadTurn]:
    DATA_DIR.mkdir(exist_ok=True)
    if not ACTIVE_TURNS_FILE.exists():
        return {}
    return {
        thread_id: ActiveThreadTurn.model_validate(active)
        for thread_id, active in json.loads(ACTIVE_TURNS_FILE.read_text()).items()
    }


def _save_active_turns(active_turns: dict[str, ActiveThreadTurn]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ACTIVE_TURNS_FILE.write_text(
        json.dumps({thread_id: active.model_dump() for thread_id, active in active_turns.items()}, indent=2) + "\n"
    )


def _load_turn_queues() -> dict[str, list[QueuedTurn]]:
    DATA_DIR.mkdir(exist_ok=True)
    if not TURN_QUEUE_FILE.exists():
        return {}
    payload = json.loads(TURN_QUEUE_FILE.read_text())
    return {
        thread_id: [QueuedTurn.model_validate(item) for item in items]
        for thread_id, items in payload.items()
    }


def _save_turn_queues(queues: dict[str, list[QueuedTurn]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    TURN_QUEUE_FILE.write_text(
        json.dumps(
            {
                thread_id: [queued.model_dump() for queued in items]
                for thread_id, items in queues.items()
                if items
            },
            indent=2,
        )
        + "\n"
    )


def _thread_queue(thread_id: str | None) -> list[QueuedTurn]:
    if not thread_id:
        return []
    return _load_turn_queues().get(thread_id, [])


def _thread_queue_depth(thread_id: str | None) -> int:
    return len(_thread_queue(thread_id))


def _enqueue_turn(
    *,
    thread_id: str,
    project_id: str,
    message: str,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    model: str | None = None,
    source: str = "web",
    reply_target: BotReplyTarget | None = None,
) -> QueuedTurn:
    queues = _load_turn_queues()
    queued = QueuedTurn(
        id=uuid.uuid4().hex[:12],
        thread_id=thread_id,
        project_id=project_id,
        message=message,
        sandbox=sandbox,
        approval_policy=approval_policy,
        model=model,
        source=source,
        reply_target=reply_target,
        created_at=time.time(),
    )
    queues.setdefault(thread_id, []).append(queued)
    _save_turn_queues(queues)
    return queued


def _pop_next_queued_turn(thread_id: str) -> QueuedTurn | None:
    queues = _load_turn_queues()
    items = queues.get(thread_id) or []
    if not items:
        return None
    queued = items.pop(0)
    if items:
        queues[thread_id] = items
    else:
        queues.pop(thread_id, None)
    _save_turn_queues(queues)
    return queued


def _pop_latest_queued_turn(thread_id: str) -> QueuedTurn | None:
    queues = _load_turn_queues()
    items = queues.get(thread_id) or []
    if not items:
        return None
    queued = items.pop()
    if items:
        queues[thread_id] = items
    else:
        queues.pop(thread_id, None)
    _save_turn_queues(queues)
    return queued


def _pop_queued_turn(thread_id: str, queued_id: str) -> QueuedTurn | None:
    queues = _load_turn_queues()
    items = queues.get(thread_id) or []
    for index, queued in enumerate(items):
        if queued.id != queued_id:
            continue
        items.pop(index)
        if items:
            queues[thread_id] = items
        else:
            queues.pop(thread_id, None)
        _save_turn_queues(queues)
        return queued
    return None


def _requeue_turn_front(queued: QueuedTurn) -> None:
    queues = _load_turn_queues()
    queues.setdefault(queued.thread_id, []).insert(0, queued)
    _save_turn_queues(queues)


def _thread_is_active(thread_id: str | None) -> bool:
    return bool(thread_id and thread_id in _load_active_turns())


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


def _mark_thread_active(
    thread_id: str | None,
    *,
    turn_id: str | None = None,
    project_id: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    source: str | None = None,
    reply_target: BotReplyTarget | None = None,
) -> None:
    if not thread_id:
        return
    now = time.time()
    active_turns = _load_active_turns()
    current = active_turns.get(thread_id)
    settings = _thread_run_settings(thread_id)
    active_turns[thread_id] = ActiveThreadTurn(
        thread_id=thread_id,
        turn_id=turn_id or (current.turn_id if current else None),
        project_id=project_id or (current.project_id if current else None),
        sandbox=sandbox or settings.sandbox or (current.sandbox if current else None),
        approval_policy=approval_policy or settings.approval_policy or (current.approval_policy if current else None),
        source=source or (current.source if current else None),
        reply_target=reply_target or (current.reply_target if current else None),
        started_at=current.started_at if current else now,
        updated_at=now,
        resume_attempts=current.resume_attempts if current else 0,
        last_resume_at=current.last_resume_at if current else None,
    )
    _save_active_turns(active_turns)


def _clear_thread_active(thread_id: str | None, turn_id: str | None = None) -> None:
    if not thread_id:
        return
    active_turns = _load_active_turns()
    active = active_turns.get(thread_id)
    if active and turn_id and active.turn_id and active.turn_id != turn_id:
        return
    if active:
        active_turns.pop(thread_id, None)
        _save_active_turns(active_turns)


def _record_thread_activity(message: dict[str, Any]) -> None:
    method = message.get("method")
    params = message.get("params") or {}
    thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
    turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
    if method in {"turn/started", "item/started"}:
        _mark_thread_active(thread_id, turn_id=turn_id)
    elif method in {"turn/completed", "turn/failed"}:
        if not IS_SHUTTING_DOWN:
            _clear_thread_active(thread_id, turn_id=turn_id)
    elif method == "thread/status/changed":
        status_type = (params.get("status") or {}).get("type")
        if status_type == "active":
            _mark_thread_active(thread_id)
        elif status_type in {"idle", "systemError", "notLoaded"}:
            if not IS_SHUTTING_DOWN:
                _clear_thread_active(thread_id)


async def _publish_queue_status(thread_id: str) -> None:
    await hub.publish(
        {
            "type": "queue.status",
            "threadId": thread_id,
            "queueDepth": _thread_queue_depth(thread_id),
            "active": _thread_is_active(thread_id),
        }
    )


async def _start_thread_turn_now(
    thread_id: str,
    *,
    project: Project,
    message: str,
    sandbox: str | None,
    approval_policy: str | None,
    model: str | None = None,
    source: str = "web",
    reply_target: BotReplyTarget | None = None,
) -> dict[str, Any]:
    await codex.request(
        "thread/resume",
        {
            "threadId": thread_id,
            **_project_params(
                project,
                {
                    "sandbox": sandbox,
                    "approvalPolicy": approval_policy,
                },
            ),
        },
    )
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": message, "text_elements": []}],
        "cwd": project.path,
    }
    if model or project.model:
        params["model"] = model or project.model
    if approval_policy:
        params["approvalPolicy"] = approval_policy
    if sandbox:
        params["sandboxPolicy"] = _sandbox_policy(sandbox, project.path)
    params["input"][0]["text"] = _with_relay_guard(params["input"][0]["text"], _turn_source_for_relay_guard(thread_id, source))
    response = await codex.request("turn/start", params)
    _mark_thread_active(
        thread_id,
        turn_id=(response.get("turn") or {}).get("id") if isinstance(response, dict) else None,
        project_id=project.id,
        sandbox=sandbox,
        approval_policy=approval_policy,
        source=source,
        reply_target=reply_target,
    )
    _append_bot_event(
        {
            "type": "turn_started",
            "thread_id": thread_id,
            "project_id": project.id,
            "source": source,
        }
    )
    await _publish_queue_status(thread_id)
    return response


async def _drain_thread_queue(thread_id: str) -> None:
    if not thread_id or _thread_is_active(thread_id):
        await _publish_queue_status(thread_id)
        return
    queued = _pop_next_queued_turn(thread_id)
    if not queued:
        await _publish_queue_status(thread_id)
        return
    queued.attempts += 1
    try:
        project = _project(queued.project_id)
        await _start_thread_turn_now(
            thread_id,
            project=project,
            message=queued.message,
            sandbox=queued.sandbox or project.sandbox,
            approval_policy=queued.approval_policy or project.approval_policy,
            model=queued.model,
            source=f"queued:{queued.source}",
            reply_target=queued.reply_target,
        )
        _append_bot_event(
            {
                "type": "queued_turn_started",
                "thread_id": thread_id,
                "queued_id": queued.id,
                "remaining": _thread_queue_depth(thread_id),
            }
        )
    except Exception as exc:
        if queued.attempts < 3:
            _requeue_turn_front(queued)
        _append_bot_event(
            {
                "type": "queued_turn_failed",
                "thread_id": thread_id,
                "queued_id": queued.id,
                "attempts": queued.attempts,
                "error": str(exc),
            }
        )
        await hub.publish(
            {
                "type": "queue.error",
                "threadId": thread_id,
                "queueDepth": _thread_queue_depth(thread_id),
                "error": str(exc),
            }
        )
    finally:
        await _publish_queue_status(thread_id)


def _schedule_queue_drain(thread_id: str | None) -> None:
    if not thread_id:
        return
    task = QUEUE_DRAIN_TASKS.get(thread_id)
    if task and not task.done():
        return
    QUEUE_DRAIN_TASKS[thread_id] = asyncio.create_task(_drain_thread_queue(thread_id))


async def _resume_active_threads_after_startup(thread_ids: set[str] | None = None) -> None:
    active_turns = _load_active_turns()
    if not active_turns:
        for thread_id in _load_turn_queues():
            _schedule_queue_drain(thread_id)
        return
    for thread_id, active in list(active_turns.items()):
        if thread_ids is not None and thread_id not in thread_ids:
            continue
        if active.resume_attempts >= 3:
            continue
        project = None
        if active.project_id:
            with contextlib.suppress(Exception):
                project = _project(active.project_id)
        if project is None:
            with contextlib.suppress(Exception):
                thread_response = await codex.request("thread/read", {"threadId": thread_id, "includeTurns": False})
                thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
                project = _project_for_cwd(thread.get("cwd"))
        if project is None:
            _clear_thread_active(thread_id)
            continue
        settings = _thread_run_settings(thread_id)
        sandbox = active.sandbox or settings.sandbox or project.sandbox
        approval_policy = active.approval_policy or settings.approval_policy or project.approval_policy
        active.resume_attempts += 1
        active.last_resume_at = time.time()
        active.updated_at = time.time()
        active_turns[thread_id] = active
        _save_active_turns(active_turns)
        try:
            response = await _start_thread_turn_now(
                thread_id,
                project=project,
                message=(
                    "codex-web was restarted while this thread had an active turn. "
                    "Continue the interrupted work from the latest available context. "
                    "Do not restart from scratch; inspect the current workspace state, infer what was in progress, "
                    "resume the next concrete step, and report only meaningful progress."
                ),
                sandbox=sandbox,
                approval_policy=approval_policy,
                source=f"restart-recovery:{active.source or 'unknown'}",
                reply_target=active.reply_target,
            )
            _mark_thread_active(
                thread_id,
                turn_id=(response.get("turn") or {}).get("id") if isinstance(response, dict) else active.turn_id,
                project_id=project.id,
                sandbox=sandbox,
                approval_policy=approval_policy,
                source=f"restart-recovery:{active.source or 'unknown'}",
                reply_target=active.reply_target,
            )
            _append_bot_event({"type": "active_thread_resumed", "thread_id": thread_id, "project_id": project.id})
        except Exception as exc:
            _append_bot_event({"type": "active_thread_resume_failed", "thread_id": thread_id, "error": str(exc)})
    for thread_id in _load_turn_queues():
        _schedule_queue_drain(thread_id)


def _append_bot_event(event: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {"created_at": time.time(), **event}
    with BOTS_EVENTS_FILE.open("a") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


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
    now = time.time()
    return _upsert_bot_binding(
        BotBinding(
            id=uuid.uuid4().hex[:12],
            connection_id=message.connection_id or source.connection_id,
            provider=source.provider,
            external_conversation_id=message.external_conversation_id,
            thread_id=source.thread_id,
            project_id=source.project_id,
            external_name=message.external_name or source.external_name,
            thread_name=source.thread_name,
            route_prefix=source.route_prefix,
            is_master=False,
            is_primary_channel=False,
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
) -> tuple[BotBinding | None, str, bool]:
    current_by_thread_id = {binding.thread_id: binding for binding in current_bindings}
    candidates = [
        binding
        for binding in _bindings_for_project(provider, project_id)
        if binding.external_conversation_id != message.external_conversation_id
    ]
    matches: dict[str, tuple[BotBinding, str]] = {}
    for binding in sorted(candidates, key=lambda item: len(_binding_prefix(item) or ""), reverse=True):
        prefix = _binding_prefix(binding)
        if not prefix:
            continue
        stripped = _strip_prefix(message.text, prefix)
        if stripped is None:
            continue
        if binding.thread_id in current_by_thread_id:
            return current_by_thread_id[binding.thread_id], stripped, False
        matches.setdefault(binding.thread_id, (binding, stripped))
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


async def _handle_bot_inbound(message: BotInboundMessage) -> dict[str, Any]:
    provider = message.provider.lower()
    bindings = _bindings_for_connection(provider, message.external_conversation_id)
    project_id = message.project_id or (bindings[0].project_id if bindings else "home")
    steer_now, route_message = _steer_route_message(message)
    exact_binding = None if steer_now else _binding_for_external_target(
        provider,
        project_id,
        message.external_conversation_id,
        message.external_thread_id,
    )
    if exact_binding is not None:
        binding = (
            exact_binding
            if exact_binding.external_conversation_id == message.external_conversation_id
            else _clone_binding_for_conversation(exact_binding, message)
        )
        routed_text = route_message.text
        route_error = False
    else:
        binding, routed_text, route_error = _resolve_bot_binding(
            bindings,
            route_message,
            prefer_external_thread=not steer_now,
            allow_master_fallback=not steer_now,
        )
    if binding is None:
        cross_binding, cross_text, cross_ambiguous = _cross_channel_binding_for_message(
            provider,
            project_id,
            bindings,
            route_message,
        )
        if cross_binding is not None:
            binding = (
                cross_binding
                if cross_binding.external_conversation_id == message.external_conversation_id
                else _clone_binding_for_conversation(cross_binding, message)
            )
            routed_text = cross_text
            route_error = False
        elif cross_ambiguous:
            route_error = True
    if route_error:
        available_prefixes = [_binding_prefix(binding) for binding in bindings]
        if not available_prefixes:
            available_prefixes = [
                _binding_prefix(binding)
                for binding in _bindings_for_project(provider, project_id)
                if _binding_prefix(binding)
            ]
        _append_bot_event(
            {
                "type": "inbound_ambiguous",
                "provider": provider,
                "external_conversation_id": message.external_conversation_id,
                "message_id": message.message_id,
                "available_prefixes": available_prefixes,
            }
        )
        return {
            "ok": False,
            "ambiguous": True,
            "availablePrefixes": available_prefixes,
        }
    if binding is None:
        binding = await _start_bot_thread(
            BotBindingCreate(
                connection_id=message.connection_id,
                provider=provider,
                external_conversation_id=message.external_conversation_id,
                project_id=project_id,
                external_name=message.external_name,
            )
        )
        routed_text = route_message.text
    elif message.connection_id and not binding.connection_id:
        binding.connection_id = message.connection_id
        binding.updated_at = time.time()
        binding = _upsert_bot_binding(binding)

    project = _project(binding.project_id)
    if not routed_text.strip():
        return {"ok": False, "empty": True, "threadId": binding.thread_id}
    reply_target = _remember_bot_reply_target(binding, message)
    if _is_details_command(routed_text):
        delivery = await _send_bot_details(binding)
        return {"ok": True, "threadId": binding.thread_id, "details": True, "delivery": delivery}
    prompt = _format_bot_prompt(message, provider, routed_text)
    if steer_now and _thread_is_active(binding.thread_id):
        with contextlib.suppress(Exception):
            await codex.request("turn/interrupt", {"threadId": binding.thread_id})
        _clear_thread_active(binding.thread_id)
    if not steer_now and (_thread_is_active(binding.thread_id) or _thread_queue_depth(binding.thread_id)):
        queued = _enqueue_turn(
            thread_id=binding.thread_id,
            project_id=project.id,
            message=prompt,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            source=provider,
            reply_target=reply_target,
        )
        binding.updated_at = time.time()
        _upsert_bot_binding(binding)
        _append_bot_event(
            {
                "type": "inbound_turn_queued",
                "provider": provider,
                "external_conversation_id": message.external_conversation_id,
                "message_id": message.message_id,
                "thread_id": binding.thread_id,
                "queued_id": queued.id,
                "queue_depth": _thread_queue_depth(binding.thread_id),
            }
        )
        await _publish_queue_status(binding.thread_id)
        await hub.publish(
            {
                "type": "bot.inbound",
                "provider": provider,
                "externalConversationId": message.external_conversation_id,
                "threadId": binding.thread_id,
                "text": routed_text,
                "senderId": message.sender_id,
                "senderName": message.sender_name,
                "messageId": message.message_id,
                "queued": True,
                "queuedId": queued.id,
                "queueDepth": _thread_queue_depth(binding.thread_id),
            }
        )
        return {
            "ok": True,
            "queued": True,
            "threadId": binding.thread_id,
            "queuedId": queued.id,
            "queueDepth": _thread_queue_depth(binding.thread_id),
        }
    for attempt in range(2):
        try:
            await codex.request(
                "thread/resume",
                {
                    "threadId": binding.thread_id,
                    **_project_params(
                        project,
                        {
                            "sandbox": binding.sandbox,
                            "approvalPolicy": binding.approval_policy,
                        },
                    ),
                },
            )
            turn = await codex.request(
                "turn/start",
                {
                    "threadId": binding.thread_id,
                    "input": [{"type": "text", "text": _with_relay_guard(prompt, provider), "text_elements": []}],
                    "cwd": project.path,
                    "approvalPolicy": binding.approval_policy,
                    "sandboxPolicy": _sandbox_policy(binding.sandbox, project.path),
                },
            )
            break
        except Exception as exc:
            if attempt or not _is_stale_thread_error(exc):
                raise
            stale_thread_id = binding.thread_id
            fallback_binding = _fallback_binding_for_stale(binding, message)
            _remove_bot_binding(binding.id)
            _forget_bot_reply_target(stale_thread_id)
            _append_bot_event(
                {
                    "type": "stale_binding_repair",
                    "provider": provider,
                    "external_conversation_id": message.external_conversation_id,
                    "old_thread_id": stale_thread_id,
                    "error": str(exc),
                }
            )
            if fallback_binding:
                binding = (
                    fallback_binding
                    if fallback_binding.external_conversation_id == message.external_conversation_id
                    else _clone_binding_for_conversation(fallback_binding, message)
                )
            else:
                binding = await _start_bot_thread(
                    BotBindingCreate(
                        connection_id=message.connection_id or binding.connection_id,
                        provider=provider,
                        external_conversation_id=message.external_conversation_id,
                        project_id=message.project_id or binding.project_id,
                        external_name=message.external_name or binding.external_name,
                        thread_name=binding.thread_name,
                        route_prefix=binding.route_prefix,
                        is_master=binding.is_master,
                        post_in_thread=binding.post_in_thread,
                        sandbox=binding.sandbox,
                        approval_policy=binding.approval_policy,
                    )
                )
            project = _project(binding.project_id)
            reply_target = _remember_bot_reply_target(binding, message) or reply_target
    _mark_thread_active(
        binding.thread_id,
        turn_id=(turn.get("turn") or {}).get("id") if isinstance(turn, dict) else None,
        project_id=binding.project_id,
        sandbox=binding.sandbox,
        approval_policy=binding.approval_policy,
        source=f"steer:{provider}" if steer_now else provider,
        reply_target=reply_target,
    )
    binding.updated_at = time.time()
    _upsert_bot_binding(binding)
    _append_bot_event(
        {
            "type": "inbound_turn_steered" if steer_now else "inbound_turn_started",
            "provider": provider,
            "external_conversation_id": message.external_conversation_id,
            "message_id": message.message_id,
            "thread_id": binding.thread_id,
            "sender_id": message.sender_id,
            "steered": steer_now,
        }
    )
    await hub.publish(
        {
            "type": "bot.inbound",
            "provider": provider,
            "externalConversationId": message.external_conversation_id,
            "threadId": binding.thread_id,
            "text": routed_text,
            "senderId": message.sender_id,
            "senderName": message.sender_name,
            "messageId": message.message_id,
            "steered": steer_now,
        }
    )
    return {"ok": True, "threadId": binding.thread_id, "turn": turn, "steered": steer_now}


def _is_stale_thread_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "no rollout found for thread id" in text or "thread not found" in text


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
) -> tuple[BotBinding | None, str, bool]:
    text = message.text
    if not bindings:
        return None, text, False
    if prefer_external_thread:
        thread_binding = _binding_for_external_thread(bindings, message.external_thread_id)
        if thread_binding:
            return thread_binding, text, False
    for binding in sorted(bindings, key=lambda item: len(_binding_prefix(item) or ""), reverse=True):
        prefix = _binding_prefix(binding)
        if not prefix:
            continue
        stripped = _strip_prefix(text, prefix)
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


def _strip_prefix(text: str, prefix: str) -> str | None:
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
    bindings = _load_bot_bindings()
    changed = False
    for binding in bindings:
        if binding.thread_id == thread_id:
            binding.thread_name = name
            binding.route_prefix = name
            binding.updated_at = time.time()
            changed = True
    if changed:
        _save_bot_bindings(bindings)
    return response


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


async def _record_bot_outbound(message: dict[str, Any]) -> None:
    if message.get("method") != "item/completed":
        return
    params = message.get("params") or {}
    item = params.get("item") or {}
    thread_id = params.get("threadId")
    if not thread_id:
        return
    detail = _format_bot_detail_item(item)
    if detail:
        _record_bot_detail(thread_id, item.get("type") or "detail", detail["title"], detail["text"])
        return
    if item.get("type") != "agentMessage":
        return
    bindings = _bindings_for_thread(thread_id)
    if not bindings:
        bindings = await _project_scoped_bindings_for_thread(thread_id)
    for binding in _outbound_bindings_for_thread(thread_id, bindings):
        prefix = _binding_prefix(binding)
        outbound_text = _format_bot_outbound_item(item, prefix)
        if not outbound_text:
            continue
        event = {
            "type": "outbound_ready",
            "provider": binding.provider,
            "external_conversation_id": binding.external_conversation_id,
            "thread_id": thread_id,
            "thread_name": binding.thread_name,
            "route_prefix": prefix,
            "item_type": item.get("type"),
            "text": outbound_text,
        }
        delivery = await _send_bot_outbound(binding, outbound_text)
        _remember_bot_delivery_target(binding, delivery)
        event["delivery"] = delivery
        _append_bot_event(event)
        await hub.publish({"type": "bot.outbound", **event})


def _is_details_command(text: str) -> bool:
    return text.strip().lower() in {"details", "detail"}


async def _send_bot_details(binding: BotBinding) -> dict[str, Any]:
    detail = _latest_bot_detail(binding.thread_id)
    if not detail:
        return await _send_bot_outbound(
            binding,
            "No command or file details are available for this thread yet.",
            reply_in_thread=True,
        )
    return await _send_bot_outbound(binding, _format_bot_detail_response(detail), reply_in_thread=True)


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


def _format_bot_outbound_item(item: dict[str, Any], prefix: str | None) -> str | None:
    item_type = item.get("type")
    label = f"{prefix}: " if prefix else ""
    if item_type == "agentMessage":
        text = (item.get("text") or "").strip()
        return f"{label}{text}" if text else None

    if item_type == "commandExecution":
        command = (item.get("command") or "").strip()
        output = (item.get("aggregatedOutput") or "").strip()
        if not command and not output:
            return None
        parts = [f"{label}Command result".strip()]
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
        parts = [f"{label}{summary}".strip()]
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
    prefix = _binding_prefix(binding)
    return f"Codex · {prefix}" if prefix else "Codex"


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


async def _send_bot_outbound(binding: BotBinding, text: str, *, reply_in_thread: bool | None = None) -> dict[str, Any]:
    connection = _bot_connection(binding.connection_id) if binding.connection_id else None
    if not connection or not connection.bot_token:
        return {"sent": False, "reason": "missing_bot_token"}
    try:
        if binding.provider == "slack":
            target = _active_reply_target_for_binding(binding) or _reply_target_for_binding(binding)
            should_thread = _should_reply_in_external_thread(binding) if reply_in_thread is None else reply_in_thread
            thread_ts = (target.external_thread_id or target.message_id) if (should_thread and target) else None
            return await asyncio.to_thread(
                _post_slack_message,
                connection.bot_token,
                binding.external_conversation_id,
                text,
                username=_slack_reply_username(binding),
                icon_emoji=_slack_reply_icon(binding),
                thread_ts=thread_ts,
            )
        if binding.provider == "telegram":
            return await asyncio.to_thread(_post_telegram_message, connection.bot_token, binding.external_conversation_id, text)
    except Exception as exc:
        return {"sent": False, "reason": str(exc)}
    return {"sent": False, "reason": "unsupported_provider"}


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


async def _record_bot_approval_request(request: dict[str, Any]) -> None:
    thread_id = _approval_thread_id(request)
    if not thread_id:
        return
    for binding in _outbound_bindings_for_thread(thread_id, _bindings_for_thread(thread_id)):
        if binding.provider != "slack" or not binding.connection_id:
            continue
        connection = _bot_connection(binding.connection_id)
        if not connection.bot_token:
            continue
        text = f"Approval requested for {_binding_prefix(binding) or thread_id}"
        target = _active_reply_target_for_binding(binding) or _reply_target_for_binding(binding)
        thread_ts = (target.external_thread_id or target.message_id) if (_should_reply_in_external_thread(binding) and target) else None
        delivery = await asyncio.to_thread(
            _post_slack_message,
            connection.bot_token,
            binding.external_conversation_id,
            text,
            username=_slack_reply_username(binding),
            icon_emoji=_slack_reply_icon(binding),
            thread_ts=thread_ts,
            blocks=_approval_blocks(request, binding),
        )
        response = delivery.get("providerResponse") or {}
        if delivery.get("sent") and response.get("ts"):
            _remember_approval_message(
                request.get("id"),
                connection_id=connection.id,
                channel=binding.external_conversation_id,
                message_ts=str(response["ts"]),
                context=_binding_prefix(binding) or thread_id,
                thread_id=thread_id,
            )
        _append_bot_event(
            {
                "type": "approval_request_sent",
                "provider": "slack",
                "external_conversation_id": binding.external_conversation_id,
                "thread_id": thread_id,
                "request_id": request.get("id"),
                "delivery": delivery,
            }
        )


async def _update_slack_approval_messages(
    request_id: int | str,
    request: dict[str, Any],
    *,
    decision: str,
    actor: str,
) -> None:
    key = str(request_id)
    messages = _load_approval_messages().get(key, [])
    status = f"{actor} selected `{decision}`."
    for message in messages:
        with contextlib.suppress(Exception):
            connection = _bot_connection(message.connection_id)
            if not connection.bot_token:
                continue
            await asyncio.to_thread(
                _update_slack_message,
                connection.bot_token,
                message.channel,
                message.message_ts,
                f"{actor} selected {decision} for approval request {request_id}.",
                blocks=_approval_resolved_blocks(request, message.context, status),
            )
    _forget_approval_messages(request_id)


async def _resolve_approval_request(request_id: int | str, decision: str, *, actor: str) -> dict[str, bool]:
    request = codex.pending_approvals.get(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Approval request not found")
    result = _approval_result(request["method"], decision)
    await codex.respond_to_server_request(request_id, result)
    await _update_slack_approval_messages(request_id, request, decision=decision, actor=actor)
    _append_bot_event(
        {
            "type": "approval_resolved",
            "request_id": request_id,
            "decision": decision,
            "actor": actor,
            "thread_id": _approval_thread_id(request),
        }
    )
    return {"ok": True}


async def _handle_slack_interaction(connection: BotConnection, payload: dict[str, Any]) -> None:
    actions = payload.get("actions") or []
    for action in actions:
        if not str(action.get("action_id") or "").startswith("codex_approval_"):
            continue
        try:
            value = json.loads(action.get("value") or "{}")
        except json.JSONDecodeError:
            continue
        request_id = _request_id_value(value.get("request_id"))
        decision = value.get("decision")
        request = codex.pending_approvals.get(request_id)
        channel = (payload.get("channel") or {}).get("id") or connection.default_external_conversation_id
        message_ts = _slack_interaction_message_ts(payload)
        user = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id") or "Slack"
        context = _slack_interaction_context(payload, str(request_id))
        if not request or not decision:
            if channel and message_ts and connection.bot_token:
                await asyncio.to_thread(
                    _update_slack_message,
                    connection.bot_token,
                    channel,
                    message_ts,
                    "That approval request is no longer pending.",
                    blocks=_approval_resolved_blocks(None, context, "Already resolved."),
                )
            return
        await _resolve_approval_request(request_id, decision, actor=user)
        _append_bot_event(
            {
                "type": "approval_resolved_from_slack",
                "provider": "slack",
                "connection_id": connection.id,
                "request_id": request_id,
                "decision": decision,
                "user": user,
            }
        )


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


class BotRuntime:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.fingerprints: dict[str, tuple[Any, ...]] = {}
        self.lock = asyncio.Lock()

    async def sync(self) -> None:
        async with self.lock:
            connections = _load_bot_connections()
            desired: dict[str, tuple[Any, ...]] = {}
            for connection in connections:
                fingerprint = self._fingerprint(connection)
                if not fingerprint:
                    continue
                desired[connection.id] = fingerprint
                if self.fingerprints.get(connection.id) == fingerprint and connection.id in self.tasks:
                    continue
                await self._stop_locked(connection.id)
                self.tasks[connection.id] = asyncio.create_task(self._run_connection(connection))
                self.fingerprints[connection.id] = fingerprint
            for connection_id in list(self.tasks):
                if connection_id not in desired:
                    await self._stop_locked(connection_id)

    async def stop(self) -> None:
        async with self.lock:
            for connection_id in list(self.tasks):
                await self._stop_locked(connection_id)

    async def _stop_locked(self, connection_id: str) -> None:
        task = self.tasks.pop(connection_id, None)
        self.fingerprints.pop(connection_id, None)
        if connection_id in BOT_RUNTIME_STATUS:
            BOT_RUNTIME_STATUS[connection_id] = {
                **BOT_RUNTIME_STATUS[connection_id],
                "status": "stopped",
                "updatedAt": time.time(),
            }
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _fingerprint(self, connection: BotConnection) -> tuple[Any, ...] | None:
        if connection.provider == "slack" and connection.bot_token and connection.slack_app_token:
            return ("slack", connection.bot_token, connection.slack_app_token)
        if connection.provider == "telegram" and connection.bot_token:
            return ("telegram", connection.bot_token, connection.telegram_update_offset)
        return None

    async def _run_connection(self, connection: BotConnection) -> None:
        while True:
            try:
                _set_runtime_status(connection, "starting", lastError=None)
                if connection.provider == "slack":
                    await self._run_slack(connection)
                elif connection.provider == "telegram":
                    await self._run_telegram(connection)
                else:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if connection.provider == "slack" and _is_transient_websocket_disconnect(exc):
                    _set_runtime_status(
                        connection,
                        "reconnecting",
                        lastDisconnect=str(exc),
                        lastDisconnectAt=time.time(),
                        lastError=None,
                    )
                    _append_bot_event(
                        {
                            "type": "runtime_reconnect",
                            "provider": connection.provider,
                            "connection_id": connection.id,
                            "reason": str(exc),
                        }
                    )
                    await hub.publish(
                        {
                            "type": "bot.runtime",
                            "provider": connection.provider,
                            "connectionId": connection.id,
                            "status": "reconnecting",
                            "reason": str(exc),
                        }
                    )
                    await asyncio.sleep(5)
                    continue
                _set_runtime_status(connection, "error", lastError=str(exc), lastErrorAt=time.time())
                _append_bot_event(
                    {
                        "type": "runtime_error",
                        "provider": connection.provider,
                        "connection_id": connection.id,
                        "error": str(exc),
                    }
                )
                await hub.publish(
                    {
                        "type": "bot.runtime",
                        "provider": connection.provider,
                        "connectionId": connection.id,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                await asyncio.sleep(10)

    async def _run_slack(self, connection: BotConnection) -> None:
        assert connection.slack_app_token
        socket_url = await asyncio.to_thread(_slack_socket_url, connection.slack_app_token)
        _set_runtime_status(connection, "connecting", lastError=None)
        await hub.publish({"type": "bot.runtime", "provider": "slack", "connectionId": connection.id, "status": "connected"})
        async with websockets.connect(socket_url, ping_interval=60, ping_timeout=None, close_timeout=5) as websocket:
            _set_runtime_status(connection, "connected", connectedAt=time.time(), lastError=None)
            async for raw in websocket:
                envelope = json.loads(raw)
                envelope_id = envelope.get("envelope_id")
                if envelope_id:
                    await websocket.send(json.dumps({"envelope_id": envelope_id}))
                payload = envelope.get("payload") or {}
                _set_runtime_status(
                    connection,
                    "connected",
                    lastEnvelopeAt=time.time(),
                    lastPayloadType=payload.get("type"),
                )
                try:
                    if payload.get("type") == "block_actions":
                        await _handle_slack_interaction(connection, payload)
                        continue
                    event = payload.get("event") or {}
                    if event:
                        _set_runtime_status(connection, "connected", lastEventAt=time.time(), lastEventType=event.get("type"))
                    if event.get("type") not in {"message", "app_mention"}:
                        continue
                    if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_deleted"}:
                        continue
                    text = _strip_slack_mentions(event.get("text") or "")
                    channel = event.get("channel") or connection.default_external_conversation_id
                    if not text or not channel:
                        continue
                    result = await _handle_bot_inbound(
                        BotInboundMessage(
                            provider="slack",
                            external_conversation_id=channel,
                            connection_id=connection.id,
                            external_name=connection.default_external_name or channel,
                            sender_id=event.get("user"),
                            text=text,
                            project_id=connection.project_id,
                            external_thread_id=event.get("thread_ts") or event.get("ts"),
                            message_id=event.get("ts"),
                        )
                    )
                    if result.get("ambiguous") and connection.bot_token:
                        binding = _first_binding_for_connection("slack", channel)
                        await asyncio.to_thread(
                            _post_slack_message,
                            connection.bot_token,
                            channel,
                            _ambiguous_route_message(result.get("availablePrefixes") or []),
                            username=_slack_reply_username(binding) if binding else None,
                            icon_emoji=_slack_reply_icon(binding) if binding else None,
                            thread_ts=event.get("thread_ts") or event.get("ts"),
                        )
                except Exception as exc:
                    event = payload.get("event") or {}
                    channel = event.get("channel") or (payload.get("channel") or {}).get("id") or connection.default_external_conversation_id
                    thread_ts = event.get("thread_ts") or event.get("ts") or ((payload.get("message") or {}).get("ts"))
                    _set_runtime_status(connection, "connected", lastError=str(exc), lastErrorAt=time.time())
                    _append_bot_event(
                        {
                            "type": "inbound_error",
                            "provider": "slack",
                            "connection_id": connection.id,
                            "external_conversation_id": channel,
                            "message_id": event.get("ts"),
                            "error": str(exc),
                        }
                    )
                    if channel and connection.bot_token:
                        binding = _first_binding_for_connection("slack", channel)
                        await asyncio.to_thread(
                            _post_slack_message,
                            connection.bot_token,
                            channel,
                            f"Codex could not handle that Slack message: {_truncate_text(str(exc), 500)}",
                            username=_slack_reply_username(binding) if binding else None,
                            icon_emoji=_slack_reply_icon(binding) if binding else None,
                            thread_ts=thread_ts,
                        )

    async def _run_telegram(self, connection: BotConnection) -> None:
        assert connection.bot_token
        offset = connection.telegram_update_offset
        _set_runtime_status(connection, "polling", connectedAt=time.time(), lastError=None)
        await hub.publish({"type": "bot.runtime", "provider": "telegram", "connectionId": connection.id, "status": "polling"})
        while True:
            url = f"https://api.telegram.org/bot{connection.bot_token}/getUpdates?timeout=0"
            if offset is not None:
                url += f"&offset={offset}"
            response = await asyncio.to_thread(_get_json, url)
            if not response.get("ok"):
                raise RuntimeError(f"Telegram getUpdates failed: {response}")
            for update in response.get("result") or []:
                _set_runtime_status(connection, "polling", lastEnvelopeAt=time.time(), lastPayloadType="update")
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = int(update_id) + 1
                message_payload = update.get("message") or update.get("edited_message") or {}
                text = (message_payload.get("text") or "").strip()
                chat = message_payload.get("chat") or {}
                chat_id = chat.get("id")
                if not text or chat_id is None:
                    continue
                sender = message_payload.get("from") or {}
                await _handle_bot_inbound(
                    BotInboundMessage(
                        provider="telegram",
                        external_conversation_id=str(chat_id),
                        external_name=chat.get("title") or chat.get("username") or str(chat_id),
                        sender_id=str(sender.get("id")) if sender.get("id") is not None else None,
                        sender_name=sender.get("username") or sender.get("first_name"),
                        text=text,
                        project_id=connection.project_id,
                        message_id=str(message_payload.get("message_id")) if message_payload.get("message_id") is not None else None,
                    )
                )
            if offset is not None:
                _update_bot_connection(connection.id, telegram_update_offset=offset)
            await asyncio.sleep(10)


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


def _codex_request_timeout(method: str) -> float | None:
    if method == "initialize":
        return 15
    if method in {"thread/list", "thread/read", "account/rateLimits/read"}:
        return 10
    if method in {"thread/resume", "thread/start", "thread/name/set"}:
        return 20
    if method == "turn/start":
        return 30
    if method == "turn/interrupt":
        return 10
    return 20


class CodexAppServer:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_approvals: dict[int | str, dict[str, Any]] = {}
        self.last_error: str | None = None
        self.write_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.lifecycle_lock:
            if self.proc and self.proc.poll() is None and self.ready.is_set():
                return
            if self.proc and self.proc.poll() is None:
                await self.stop()
            self.ready.clear()
            self.last_error = None
            self._fail_pending(RuntimeError("Codex app-server restarted"))
            self.proc = subprocess.Popen(
                ["codex", "app-server"],
                cwd=str(Path.home()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.reader_task = asyncio.create_task(self._read_loop())
            self.stderr_task = asyncio.create_task(self._stderr_loop())
            try:
                init = await asyncio.wait_for(
                    self.request(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "codex_web_local",
                                "title": "Codex Web Local",
                                "version": "0.1.0",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                    ),
                    timeout=15,
                )
                await self.notify("initialized", {})
                self.ready.set()
                await hub.publish({"type": "codex.ready", "initialize": init})
            except Exception as exc:
                self.last_error = str(exc)
                await hub.publish({"type": "codex.error", "error": self.last_error})
                raise

    async def stop(self) -> None:
        self._fail_pending(RuntimeError("Codex app-server stopped"))
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.proc.wait)
        self.proc = None
        self.ready.clear()
        for task in (self.reader_task, self.stderr_task):
            if task and not task.done():
                task.cancel()
        for task in (self.reader_task, self.stderr_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.reader_task = None
        self.stderr_task = None

    def _fail_pending(self, exc: Exception) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
        self.pending.clear()

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await asyncio.to_thread(self.proc.stderr.readline)
            if not line:
                return
            text = line.rstrip("\n")
            self.last_error = text
            print(f"codex app-server stderr: {text}", flush=True)
            await hub.publish({"type": "codex.stderr", "text": text})

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await asyncio.to_thread(self.proc.stdout.readline)
            if not line:
                self.ready.clear()
                self.last_error = "Codex app-server stopped"
                self._fail_pending(RuntimeError(self.last_error))
                print(f"codex app-server stdout closed: {self.last_error}", flush=True)
                if self.proc and self.proc.poll() is not None:
                    self.proc = None
                await hub.publish({"type": "codex.closed"})
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                await hub.publish({"type": "codex.raw", "text": line.rstrip("\n")})
                continue

            message_id = message.get("id")
            if message_id is not None and "method" not in message:
                future = self.pending.pop(message_id, None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(message["error"]))
                    else:
                        future.set_result(message.get("result", {}))
                await hub.publish({"type": "rpc.response", "message": message})
                continue

            if message_id is not None and "method" in message:
                approval_thread_id = _approval_thread_id(message)
                approval_settings = _approval_run_settings(message)
                if approval_settings.approval_policy == "never":
                    result = _approval_result(message["method"], "acceptForSession")
                    await self._send({"id": message_id, "result": result})
                    await hub.publish({"type": "approval.auto_resolved", "id": message_id, "result": result})
                    _append_bot_event(
                        {
                            "type": "approval_auto_resolved",
                            "thread_id": approval_thread_id,
                            "request_id": message_id,
                            "method": message.get("method"),
                            "approval_policy": approval_settings.approval_policy,
                        }
                    )
                    continue
                self.pending_approvals[message_id] = message
                await _record_bot_approval_request(message)
                await hub.publish({"type": "approval.request", "request": message})
                continue

            _record_thread_activity(message)
            method = message.get("method")
            params = message.get("params") or {}
            thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
            if method in {"turn/completed", "turn/failed"}:
                _schedule_queue_drain(thread_id)
            elif method == "thread/status/changed":
                status_type = (params.get("status") or {}).get("type")
                if status_type in {"idle", "systemError", "notLoaded"}:
                    _schedule_queue_drain(thread_id)
            await _record_bot_outbound(message)
            await hub.publish({"type": "codex.event", "message": message})

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise HTTPException(status_code=503, detail="Codex app-server is not running")
        async with self.write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        await self.ensure_started(method == "initialize")
        message_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[message_id] = future
        message = {"method": method, "id": message_id, "params": params}
        await self._send(message)
        timeout = _codex_request_timeout(method)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(message_id, None)
            self.last_error = f"{method} timed out after {timeout}s"
            print(f"codex app-server request timed out: {self.last_error}", flush=True)
            await hub.publish({"type": "codex.error", "error": self.last_error})
            raise HTTPException(status_code=504, detail=self.last_error) from exc

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params})

    async def ensure_started(self, skip: bool = False) -> None:
        if skip:
            return
        if (
            not self.proc
            or self.proc.poll() is not None
            or (self.reader_task is not None and self.reader_task.done() and not self.ready.is_set())
        ):
            await self.start()
        elif not self.ready.is_set():
            await self.ready.wait()

    async def respond_to_server_request(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.pending_approvals.pop(request_id, None)
        await self._send({"id": request_id, "result": result})
        await hub.publish({"type": "approval.resolved", "id": request_id, "result": result})


codex = CodexAppServer()
bot_runtime = BotRuntime()
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


def _daemon_health() -> dict[str, Any]:
    now = time.time()
    problems: list[str] = []
    configured_runtime_ids = set(bot_runtime.fingerprints)
    running_runtime_ids = set(bot_runtime.tasks)

    if not codex.proc or codex.proc.poll() is not None:
        problems.append("codex app-server process is not running")

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

    return {
        "ok": not problems,
        "problems": problems,
        "codexReady": codex.ready.is_set(),
        "codexPid": codex.proc.pid if codex.proc else None,
        "runtimeConnections": len(bot_runtime.tasks),
        "runtimeStatus": list(BOT_RUNTIME_STATUS.values()),
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


def _recent_bot_events(limit: int = 80) -> list[dict[str, Any]]:
    if not BOTS_EVENTS_FILE.exists():
        return []
    lines = BOTS_EVENTS_FILE.read_text(errors="replace").splitlines()[-max(1, min(limit, 300)) :]
    events: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(Exception):
            events.append(json.loads(line))
    return events


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

    exact_binding = None if steer_now else _binding_for_external_target(
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
            prefer_external_thread=not steer_now,
            allow_master_fallback=not steer_now,
        )
        if binding:
            route_source = "connection-prefix-or-primary"

    if binding is None:
        cross_binding, cross_text, cross_ambiguous = _cross_channel_binding_for_message(
            provider,
            project_id,
            bindings,
            route_message,
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
            _sd_notify("STATUS=codex-web unhealthy: " + "; ".join(health["problems"]))
        await asyncio.sleep(interval)


@app.on_event("startup")
async def startup() -> None:
    global WATCHDOG_TASK, IS_SHUTTING_DOWN
    IS_SHUTTING_DOWN = False
    _load_projects()
    _dedupe_bot_integrations()
    try:
        await codex.start()
    except Exception:
        # Keep the HTTP UI up so it can report the app-server failure.
        pass
    await bot_runtime.sync()
    if codex.ready.is_set():
        asyncio.create_task(_resume_active_threads_after_startup())
    _sd_notify("READY=1\nSTATUS=codex-web started")
    WATCHDOG_TASK = asyncio.create_task(_watchdog_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global WATCHDOG_TASK, IS_SHUTTING_DOWN
    IS_SHUTTING_DOWN = True
    _sd_notify("STOPPING=1\nSTATUS=codex-web stopping")
    if WATCHDOG_TASK:
        WATCHDOG_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await WATCHDOG_TASK
        WATCHDOG_TASK = None
    await bot_runtime.stop()
    await codex.stop()


@app.get("/")
async def index() -> HTMLResponse:
    version = _static_version()
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace('href="static/styles.css"', f'href="static/styles.css?v={version}"')
    html = html.replace('src="static/app.js"', f'src="static/app.js?v={version}"')
    return HTMLResponse(html)


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


@app.get("/api/diagnostics")
async def diagnostics(project_id: str | None = None) -> dict[str, Any]:
    return _diagnostic_snapshot(project_id)


@app.post("/api/diagnostics/route-test")
async def diagnostics_route_test(payload: BotRouteTest) -> dict[str, Any]:
    return _preview_bot_route(payload)


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
        if now - active.updated_at > 120
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


@app.get("/api/bots")
async def bot_status() -> dict[str, Any]:
    bindings = _load_bot_bindings()
    connections = _load_bot_connections()
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
    result = await codex.request("thread/list", params)
    if archived:
        return result

    items = result.get("data") or result.get("threads") or []
    existing_ids = {item.get("id") for item in items if isinstance(item, dict)}
    search_term = search.casefold() if search else None
    for indexed in _load_thread_index():
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
            "updatedAt": indexed.updatedAt or 0,
            "status": {"type": "notLoaded"},
            "turns": [],
        }
        with contextlib.suppress(Exception):
            thread_response = await codex.request("thread/read", {"threadId": indexed.id, "includeTurns": False})
            thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
            item.update(thread)
            item["name"] = indexed.name
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
        )
    return response


@app.get("/api/threads/{thread_id}")
async def read_thread(thread_id: str) -> dict[str, Any]:
    return await codex.request("thread/read", {"threadId": thread_id, "includeTurns": True})


@app.post("/api/threads/{thread_id}/name")
async def rename_thread(thread_id: str, payload: ThreadRename) -> dict[str, Any]:
    await _set_thread_name(thread_id, payload.name)
    return {"ok": True, "threadId": thread_id, "name": payload.name}


@app.post("/api/threads/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    project_id: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
) -> dict[str, Any]:
    project = _project(project_id)
    remembered = _thread_run_settings(thread_id)
    effective_sandbox = sandbox or remembered.sandbox or project.sandbox
    effective_approval_policy = approval_policy or remembered.approval_policy or project.approval_policy
    _remember_thread_run_settings(thread_id, sandbox=effective_sandbox, approval_policy=effective_approval_policy)
    params = {
        "threadId": thread_id,
        **_project_params(
            project,
            {
                "sandbox": effective_sandbox,
                "approvalPolicy": effective_approval_policy,
            },
        ),
    }
    return await codex.request("thread/resume", params)


@app.post("/api/threads/{thread_id}/turns")
async def start_turn(thread_id: str, payload: TurnCreate) -> dict[str, Any]:
    project = _project(payload.project_id)
    remembered = _thread_run_settings(thread_id)
    effective_sandbox = payload.sandbox or remembered.sandbox or project.sandbox
    effective_approval_policy = payload.approval_policy or remembered.approval_policy or project.approval_policy
    _remember_thread_run_settings(thread_id, sandbox=effective_sandbox, approval_policy=effective_approval_policy)
    if _thread_is_active(thread_id) or _thread_queue_depth(thread_id):
        queued = _enqueue_turn(
            thread_id=thread_id,
            project_id=project.id,
            message=payload.message,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=payload.model,
        )
        await _publish_queue_status(thread_id)
        return {
            "queued": True,
            "queuedId": queued.id,
            "queueDepth": _thread_queue_depth(thread_id),
            "threadId": thread_id,
        }
    return await _start_thread_turn_now(
        thread_id,
        project=project,
        message=payload.message,
        sandbox=effective_sandbox,
        approval_policy=effective_approval_policy,
        model=payload.model,
    )


@app.get("/api/threads/{thread_id}/queue")
async def thread_queue(thread_id: str) -> dict[str, Any]:
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
    queued = _pop_latest_queued_turn(thread_id)
    if not queued:
        raise HTTPException(status_code=404, detail="No queued message for this thread")
    return await _steer_queued_turn(thread_id, queued)


@app.post("/api/threads/{thread_id}/queue/{queued_id}/steer")
async def steer_specific_queued_turn(thread_id: str, queued_id: str) -> dict[str, Any]:
    queued = _pop_queued_turn(thread_id, queued_id)
    if not queued:
        raise HTTPException(status_code=404, detail="Queued message not found for this thread")
    return await _steer_queued_turn(thread_id, queued)


async def _steer_queued_turn(thread_id: str, queued: QueuedTurn) -> dict[str, Any]:
    if _thread_is_active(thread_id):
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
    settings = _remember_thread_run_settings(
        thread_id,
        sandbox=payload.sandbox,
        approval_policy=payload.approval_policy,
    )
    return {"ok": True, "threadId": thread_id, **settings.model_dump()}


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
