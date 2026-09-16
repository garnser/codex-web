from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import APIRouter, FastAPI


class JsonFormatter(logging.Formatter):
    """Compact JSON formatter with optional structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update({key: value for key, value in structured.items() if value is not None})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging() -> None:
    level_name = (os.environ.get("CODEX_WEB_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "_codex_web_handler", False):
            handler.setLevel(level)
            return
    handler = logging.StreamHandler()
    handler._codex_web_handler = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    logger.log(level, message, extra={"structured": {"event": event, **fields}})


class RuntimeMetrics:
    """Small in-process metrics registry for local diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._timers: dict[str, dict[str, float]] = {}
        self.started_at = time.time()

    def increment(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, duration_seconds: float) -> None:
        with self._lock:
            current = self._timers.setdefault(
                name,
                {"count": 0.0, "totalSeconds": 0.0, "maxSeconds": 0.0},
            )
            current["count"] += 1
            current["totalSeconds"] += duration_seconds
            current["maxSeconds"] = max(current["maxSeconds"], duration_seconds)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - started)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            timers = {
                name: {
                    **values,
                    "averageSeconds": (
                        values["totalSeconds"] / values["count"]
                        if values["count"]
                        else 0.0
                    ),
                }
                for name, values in sorted(self._timers.items())
            }
        return {
            "uptimeSeconds": max(0.0, time.time() - self.started_at),
            "counters": counters,
            "timers": timers,
        }


def install_observability(app: FastAPI, host: Any) -> RuntimeMetrics:
    existing = getattr(app.state, "runtime_metrics", None)
    if isinstance(existing, RuntimeMetrics):
        return existing

    configure_logging()
    metrics = RuntimeMetrics()
    app.state.runtime_metrics = metrics

    if hasattr(host.hub, "configure_observability"):
        host.hub.configure_observability(metrics)

    codex_runtime = getattr(app.state, "codex_runtime", None)
    if codex_runtime is not None:
        codex_runtime.metrics = metrics

    router = APIRouter()

    @router.get("/api/metrics")
    async def runtime_metrics() -> dict[str, Any]:
        return metrics.snapshot()

    app.include_router(router)
    return metrics
