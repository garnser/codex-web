from __future__ import annotations

from typing import Any


class ApprovalService:
    def __init__(self, host: Any) -> None:
        self.host = host

    def list(self) -> list[dict[str, Any]]:
        return list(self.host.codex.pending_approvals.values())

    async def decide(self, request_id: str, decision: str) -> dict[str, bool]:
        normalized_id = self.host._request_id_value(request_id)
        return await self.host._resolve_approval_request(
            normalized_id,
            decision,
            actor="Codex Web",
        )
