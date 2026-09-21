from __future__ import annotations

import contextlib
import json
import time
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
        self._recent_metrics = {
            "bytesRead": 0,
            "chunksRead": 0,
            "linesConsidered": 0,
            "validEvents": 0,
            "fileSize": 0,
        }

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

    def recent_metrics(self) -> dict[str, int]:
        return dict(self._recent_metrics)

    def recent(
        self,
        limit: int = 80,
        *,
        chunk_size: int = 64 * 1024,
    ) -> list[dict[str, Any]]:
        requested = max(1, min(int(limit), 300))
        chunk_size = max(1024, int(chunk_size))
        metrics = {
            "bytesRead": 0,
            "chunksRead": 0,
            "linesConsidered": 0,
            "validEvents": 0,
            "fileSize": 0,
        }
        if not self.events_file.exists():
            self._recent_metrics = metrics
            return []

        events_reverse: list[dict[str, Any]] = []
        with self.events_file.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            metrics["fileSize"] = position
            carry = b""

            while position > 0 and len(events_reverse) < requested:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size)
                metrics["bytesRead"] += len(block)
                metrics["chunksRead"] += 1
                data = block + carry
                parts = data.split(b"\n")

                if position > 0:
                    carry = parts[0]
                    complete = parts[1:]
                else:
                    carry = b""
                    complete = parts

                for raw in reversed(complete):
                    if len(events_reverse) >= requested:
                        break
                    if not raw.strip():
                        continue
                    metrics["linesConsidered"] += 1
                    with contextlib.suppress(Exception):
                        value = json.loads(
                            raw.decode("utf-8", errors="replace")
                        )
                        if isinstance(value, dict):
                            events_reverse.append(value)

            # A file with no newline can leave its only record in carry until
            # the first/only block reaches offset zero. The position==0 branch
            # above normally consumes it; this is a defensive fallback for
            # unusual file-like behavior.
            if (
                position == 0
                and carry.strip()
                and len(events_reverse) < requested
            ):
                metrics["linesConsidered"] += 1
                with contextlib.suppress(Exception):
                    value = json.loads(
                        carry.decode("utf-8", errors="replace")
                    )
                    if isinstance(value, dict):
                        events_reverse.append(value)

        metrics["validEvents"] = len(events_reverse)
        self._recent_metrics = metrics
        return list(reversed(events_reverse))

    def thread_recent_activity_age_seconds(
        self,
        thread_id: str | None,
        *,
        limit: int = 200,
        now: float | None = None,
    ) -> float | None:
        if not thread_id:
            return None
        current = time.time() if now is None else float(now)
        activity_types = {
            "turn_started",
            "queued_turn_started",
            "outbound_ready",
            "inbound_turn_started",
            "inbound_turn_steered",
            "owner_work_watchdog_dispatched",
            "release_gate_watchdog_dispatched",
        }
        for event in reversed(self.recent(limit)):
            if event.get("thread_id") != thread_id:
                continue
            if event.get("type") not in activity_types:
                continue
            created_at = event.get("created_at")
            if isinstance(created_at, (int, float)):
                return max(0.0, current - float(created_at))
        return None

    def thread_recent_event_count(
        self,
        thread_id: str | None,
        event_types: set[str],
        *,
        within_seconds: float = 1800.0,
        limit: int = 400,
        now: float | None = None,
    ) -> int:
        if not thread_id:
            return 0
        current = time.time() if now is None else float(now)
        count = 0
        for event in reversed(self.recent(limit)):
            if event.get("thread_id") != thread_id:
                continue
            if event.get("type") not in event_types:
                continue
            created_at = event.get("created_at")
            if not isinstance(created_at, (int, float)):
                continue
            if current - float(created_at) > within_seconds:
                continue
            count += 1
        return count


def install_bot_runtime_telemetry(
    app: Any,
    host: Any,
    *,
    events_file: Path | None = None,
    status: dict[str, dict[str, Any]] | None = None,
) -> BotRuntimeTelemetry:
    if events_file is None:
        events_file = getattr(host, "BOTS_EVENTS_FILE", None)
    if events_file is None:
        raise TypeError("bot runtime telemetry requires an events file")
    telemetry = BotRuntimeTelemetry(
        events_file=events_file,
        status=(
            status
            if status is not None
            else getattr(host, "BOT_RUNTIME_STATUS", None)
        ),
    )
    app.state.bot_runtime_telemetry = telemetry
    host.BOT_RUNTIME_STATUS = telemetry.status
    host._append_bot_event = telemetry.append
    host._set_runtime_status = telemetry.set_status
    return telemetry
