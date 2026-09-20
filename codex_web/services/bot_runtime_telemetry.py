from __future__ import annotations

import contextlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from codex_web.models import BotConnection


class BotRuntimeTelemetry:
    """Own mutable bot runtime status and the local provider event journal."""

    def __init__(
        self,
        *,
        events_file: Path,
        status: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.events_file = events_file
        self.status = status if status is not None else {}

    def append(self, event: dict[str, Any]) -> None:
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"created_at": time.time(), **event}
        with self.events_file.open("a") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def set_status(
        self,
        connection: BotConnection,
        status: str,
        **details: Any,
    ) -> dict[str, Any]:
        current = dict(self.status.get(connection.id, {}))
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
        self.status[connection.id] = current
        return current

    def remove_status(self, connection_id: str) -> None:
        self.status.pop(connection_id, None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            connection_id: dict(value)
            for connection_id, value in self.status.items()
        }

    def recent(self, limit: int = 80) -> list[dict[str, Any]]:
        if not self.events_file.exists():
            return []
        with self.events_file.open(errors="replace") as handle:
            lines = deque(handle, maxlen=max(1, min(int(limit), 300)))
        events: list[dict[str, Any]] = []
        for line in lines:
            with contextlib.suppress(Exception):
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        return events


def install_bot_runtime_telemetry(app: Any, host: Any) -> BotRuntimeTelemetry:
    telemetry = BotRuntimeTelemetry(
        events_file=host.BOTS_EVENTS_FILE,
        status=getattr(host, "BOT_RUNTIME_STATUS", None),
    )
    app.state.bot_runtime_telemetry = telemetry
    host.BOT_RUNTIME_STATUS = telemetry.status
    host._append_bot_event = telemetry.append
    host._set_runtime_status = telemetry.set_status
    return telemetry
