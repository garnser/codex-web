from __future__ import annotations

from typing import Any


class RuntimeService:
    def __init__(self, host: Any) -> None:
        self.host = host

    async def status(self) -> dict[str, Any]:
        try:
            await self.host.codex.ensure_started()
        except Exception:
            pass
        return {
            "ok": self.host.codex.ready.is_set(),
            "pid": self.host.codex.proc.pid if self.host.codex.proc else None,
            "error": None if self.host.codex.ready.is_set() else self.host.codex.last_error,
            "version": self.host._static_version(),
            "pendingApprovals": list(self.host.codex.pending_approvals.values()),
            "activeTurns": len(self.host._load_active_turns()),
            "queuedTurns": sum(len(items) for items in self.host._load_turn_queues().values()),
        }

    def health(self) -> dict[str, Any]:
        return self.host._daemon_health()

    async def rate_limits(self) -> dict[str, Any]:
        return await self.host.codex.request("account/rateLimits/read")

    async def models(self, *, include_hidden: bool = False) -> dict[str, Any]:
        try:
            return await self.host.codex.request(
                "model/list",
                {"includeHidden": include_hidden, "limit": 100},
            )
        except Exception as exc:
            self.host._append_bot_event({"type": "model_list_failed", "error": str(exc)})
            return {"data": [], "nextCursor": None, "error": str(exc)}
