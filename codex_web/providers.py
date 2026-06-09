from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def post_slack_message(
    token: str,
    channel: str,
    text: str,
    *,
    username: str | None = None,
    icon_emoji: str | None = None,
    thread_ts: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if username:
        payload["username"] = username[:80]
    if icon_emoji:
        payload["icon_emoji"] = icon_emoji
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    response = post_json(
        "https://slack.com/api/chat.postMessage",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    if not response.get("ok") and (username or icon_emoji):
        fallback_payload = {"channel": channel, "text": text}
        if thread_ts:
            fallback_payload["thread_ts"] = thread_ts
        if blocks:
            fallback_payload["blocks"] = blocks
        response = post_json(
            "https://slack.com/api/chat.postMessage",
            fallback_payload,
            {"Authorization": f"Bearer {token}"},
        )
    return {"sent": bool(response.get("ok")), "providerResponse": response}


def update_slack_message(
    token: str,
    channel: str,
    message_ts: str,
    text: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"channel": channel, "ts": message_ts, "text": text}
    if blocks:
        payload["blocks"] = blocks
    response = post_json(
        "https://slack.com/api/chat.update",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    return {"sent": bool(response.get("ok")), "providerResponse": response}


def post_telegram_message(token: str, chat_id: str, text: str) -> dict[str, Any]:
    response = post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": text},
    )
    return {"sent": bool(response.get("ok")), "providerResponse": response}


def slack_socket_url(app_token: str) -> str:
    response = post_json(
        "https://slack.com/api/apps.connections.open",
        {},
        {"Authorization": f"Bearer {app_token}"},
    )
    if not response.get("ok") or not response.get("url"):
        raise RuntimeError(f"Slack Socket Mode connection failed: {response}")
    return str(response["url"])
