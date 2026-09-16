from __future__ import annotations

import time
from typing import Any

from codex_web.models import ApprovalSlackMessage


class ApprovalService:
    def __init__(self, host: Any) -> None:
        self.host = host
        # ApprovalService is already composed by application.py. Rebind the
        # historical mutation seams here so this extraction does not require a
        # second application-composition edit.
        host._remember_approval_message = self.remember_message
        host._forget_approval_messages = self.forget_messages

    def list(self) -> list[dict[str, Any]]:
        return list(self.host.codex.pending_approvals.values())

    async def decide(self, request_id: str, decision: str) -> dict[str, bool]:
        normalized_id = self.host._request_id_value(request_id)
        return await self.host._resolve_approval_request(
            normalized_id,
            decision,
            actor="Codex Web",
        )

    def remember_message(
        self,
        request_id: int | str,
        *,
        connection_id: str,
        channel: str,
        message_ts: str,
        context: str,
        thread_id: str | None = None,
    ) -> None:
        messages = self.host._load_approval_messages()
        key = str(request_id)
        current = messages.setdefault(key, [])
        if any(
            item.connection_id == connection_id
            and item.channel == channel
            and item.message_ts == message_ts
            for item in current
        ):
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
        self.host._save_approval_messages(messages)

    def forget_messages(self, request_id: int | str) -> None:
        messages = self.host._load_approval_messages()
        if messages.pop(str(request_id), None) is not None:
            self.host._save_approval_messages(messages)
