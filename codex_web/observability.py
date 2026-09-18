from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterator, Mapping, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.identity import PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService


CORRELATION_HEADER = "x-correlation-id"
CAUSATION_HEADER = "x-causation-id"
MAX_CORRELATION_LENGTH = 128
MAX_LABEL_VALUE_LENGTH = 80
MAX_RECENT_SPANS = 200
MAX_RECENT_LOGS = 500
MAX_LOG_QUERY_RESULTS = 200
MAX_LOG_RETENTION_SECONDS = 3600.0
SAFE_LOG_STRUCTURED_FIELDS = frozenset(
    {
        "event",
        "component",
        "operation",
        "provider",
        "result",
        "status",
        "status_class",
        "queue",
        "kind",
        "project_id",
        "resource_id",
        "resource_ids",
        "code",
        "method",
    }
)
SAFE_METRIC_LABELS = frozenset(
    {
        "component",
        "method",
        "operation",
        "provider",
        "result",
        "status_class",
        "queue",
        "kind",
    }
)
SENSITIVE_FIELD_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "prompt",
    "secret",
)
SENSITIVE_TOKEN_KEYS = frozenset(
    {
        "access_token",
        "api_token",
        "bearer_token",
        "id_token",
        "refresh_token",
        "service_token",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    correlation_id: str
    causation_id: str | None = None
    tenant_id: str | None = None
    workspace_id: str | None = None
    work_item_ref: str | None = None
    execution_id: str | None = None
    action_intent_id: str | None = None

    def child(self, *, causation_id: str | None = None, **updates: Any) -> "CorrelationContext":
        values = {
            "correlation_id": self.correlation_id,
            "causation_id": causation_id or self.causation_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "work_item_ref": self.work_item_ref,
            "execution_id": self.execution_id,
            "action_intent_id": self.action_intent_id,
        }
        values.update({key: value for key, value in updates.items() if value is not None})
        return CorrelationContext(**values)


_current_correlation: ContextVar[CorrelationContext | None] = ContextVar(
    "codex_web_correlation_context",
    default=None,
)


def _bounded_identifier(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > MAX_CORRELATION_LENGTH:
        return None
    return normalized


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current_correlation() -> CorrelationContext | None:
    return _current_correlation.get()


def bind_correlation(context: CorrelationContext) -> Token[CorrelationContext | None]:
    return _current_correlation.set(context)


def reset_correlation(token: Token[CorrelationContext | None]) -> None:
    _current_correlation.reset(token)


@contextmanager
def correlated(
    *,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    **scope: Any,
) -> Iterator[CorrelationContext]:
    parent = current_correlation()
    context = CorrelationContext(
        correlation_id=(
            _bounded_identifier(correlation_id)
            or (parent.correlation_id if parent else new_correlation_id())
        ),
        causation_id=_bounded_identifier(causation_id) or (parent.causation_id if parent else None),
        tenant_id=scope.get("tenant_id") or (parent.tenant_id if parent else None),
        workspace_id=scope.get("workspace_id") or (parent.workspace_id if parent else None),
        work_item_ref=scope.get("work_item_ref") or (parent.work_item_ref if parent else None),
        execution_id=scope.get("execution_id") or (parent.execution_id if parent else None),
        action_intent_id=scope.get("action_intent_id") or (parent.action_intent_id if parent else None),
    )
    token = bind_correlation(context)
    try:
        yield context
    finally:
        reset_correlation(token)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    if lowered in SENSITIVE_TOKEN_KEYS or lowered.endswith("_credential"):
        return True
    return any(fragment in lowered for fragment in SENSITIVE_FIELD_FRAGMENTS)


def sanitize_telemetry(value: Any, *, key: str = "") -> Any:
    """Remove secret/prompt material while retaining deterministic diagnostics."""
    if key and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_telemetry(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_telemetry(item) for item in value]
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    return value


def correlation_fields(context: CorrelationContext | None = None) -> dict[str, str]:
    context = context or current_correlation()
    if context is None:
        return {}
    return {
        key: value
        for key, value in {
            "correlation_id": context.correlation_id,
            "causation_id": context.causation_id,
            "tenant_id": context.tenant_id,
            "workspace_id": context.workspace_id,
            "work_item_ref": context.work_item_ref,
            "execution_id": context.execution_id,
            "action_intent_id": context.action_intent_id,
        }.items()
        if value is not None
    }


class JsonFormatter(logging.Formatter):
    """Compact secret-safe JSON formatter with correlated structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **correlation_fields(),
        }
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update({key: value for key, value in structured.items() if value is not None})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(sanitize_telemetry(payload), separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class RuntimeLogEntry:
    timestamp: float
    level: str
    logger: str
    event: str | None
    tenant_id: str | None
    workspace_id: str | None
    correlation_id: str | None
    causation_id: str | None
    work_item_ref: str | None
    execution_id: str | None
    action_intent_id: str | None
    fields: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "event": self.event,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "workItemRef": self.work_item_ref,
            "executionId": self.execution_id,
            "actionIntentId": self.action_intent_id,
            "fields": self.fields,
        }


class RuntimeLogStore:
    """Bounded structured runtime telemetry; never canonical business state."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_RECENT_LOGS,
        max_age_seconds: float = MAX_LOG_RETENTION_SECONDS,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_age_seconds = max(1.0, float(max_age_seconds))
        self._lock = threading.Lock()
        self._items: deque[RuntimeLogEntry] = deque(maxlen=self.max_entries)

    def append(self, record: logging.LogRecord) -> None:
        structured = getattr(record, "structured", None)
        values = dict(structured) if isinstance(structured, dict) else {}
        context = correlation_fields()
        for key, value in context.items():
            values.setdefault(key, value)
        safe = sanitize_telemetry(values)
        fields = {
            key: safe[key]
            for key in SAFE_LOG_STRUCTURED_FIELDS
            if key in safe and key != "event"
        }
        entry = RuntimeLogEntry(
            timestamp=float(record.created),
            level=str(record.levelname).upper(),
            logger=str(record.name),
            event=(str(safe["event"]) if safe.get("event") is not None else None),
            tenant_id=(
                str(safe["tenant_id"])
                if safe.get("tenant_id") is not None
                else None
            ),
            workspace_id=(
                str(safe["workspace_id"])
                if safe.get("workspace_id") is not None
                else None
            ),
            correlation_id=(
                str(safe["correlation_id"])
                if safe.get("correlation_id") is not None
                else None
            ),
            causation_id=(
                str(safe["causation_id"])
                if safe.get("causation_id") is not None
                else None
            ),
            work_item_ref=(
                str(safe["work_item_ref"])
                if safe.get("work_item_ref") is not None
                else None
            ),
            execution_id=(
                str(safe["execution_id"])
                if safe.get("execution_id") is not None
                else None
            ),
            action_intent_id=(
                str(safe["action_intent_id"])
                if safe.get("action_intent_id") is not None
                else None
            ),
            fields=fields,
        )
        cutoff = time.time() - self.max_age_seconds
        with self._lock:
            while self._items and self._items[0].timestamp < cutoff:
                self._items.popleft()
            self._items.append(entry)

    def query(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        window_seconds: float = 900.0,
        limit: int = 100,
        level: str | None = None,
        logger: str | None = None,
        event: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        work_item_ref: str | None = None,
        execution_id: str | None = None,
        action_intent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        window = float(window_seconds)
        if window <= 0 or window > self.max_age_seconds:
            raise ValueError(
                f"window_seconds must be between 0 and {int(self.max_age_seconds)}"
            )
        bounded_limit = int(limit)
        if bounded_limit <= 0 or bounded_limit > MAX_LOG_QUERY_RESULTS:
            raise ValueError(
                f"limit must be between 1 and {MAX_LOG_QUERY_RESULTS}"
            )
        level_filter = str(level).upper() if level else None
        logger_filter = str(logger) if logger else None
        event_filter = str(event) if event else None
        cutoff = time.time() - window
        with self._lock:
            items = list(self._items)
        rows: list[RuntimeLogEntry] = []
        for item in reversed(items):
            if item.timestamp < cutoff:
                break
            # Unscoped process logs and other tenants are deliberately excluded.
            if (
                item.tenant_id != tenant_id
                or item.workspace_id != workspace_id
            ):
                continue
            if level_filter and item.level != level_filter:
                continue
            if logger_filter and item.logger != logger_filter:
                continue
            if event_filter and item.event != event_filter:
                continue
            if correlation_id and item.correlation_id != correlation_id:
                continue
            if causation_id and item.causation_id != causation_id:
                continue
            if work_item_ref and item.work_item_ref != work_item_ref:
                continue
            if execution_id and item.execution_id != execution_id:
                continue
            if action_intent_id and item.action_intent_id != action_intent_id:
                continue
            rows.append(item)
            if len(rows) >= bounded_limit:
                break
        return [item.public() for item in rows]


class RuntimeLogHandler(logging.Handler):
    def __init__(self, store: RuntimeLogStore) -> None:
        super().__init__()
        self.store = store
        self._codex_web_runtime_log_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.append(record)
        except Exception:
            # Diagnostics capture must never break the primary application path.
            self.handleError(record)


_RUNTIME_LOG_STORE = RuntimeLogStore()


def configure_logging() -> None:
    level_name = (os.environ.get("CODEX_WEB_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    stream = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "_codex_web_handler", False)
        ),
        None,
    )
    if stream is None:
        stream = logging.StreamHandler()
        stream._codex_web_handler = True  # type: ignore[attr-defined]
        stream.setFormatter(JsonFormatter())
        root.addHandler(stream)
    stream.setLevel(level)

    runtime_handler = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "_codex_web_runtime_log_handler", False)
        ),
        None,
    )
    if runtime_handler is None:
        runtime_handler = RuntimeLogHandler(_RUNTIME_LOG_STORE)
        root.addHandler(runtime_handler)
    runtime_handler.setLevel(level)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    logger.log(
        level,
        message,
        extra={"structured": sanitize_telemetry({"event": event, **correlation_fields(), **fields})},
    )


def _metric_key(name: str, labels: Mapping[str, str] | None = None) -> str:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("metric name must not be empty")
    if not labels:
        return normalized_name
    unexpected = sorted(set(labels) - SAFE_METRIC_LABELS)
    if unexpected:
        raise ValueError(f"unsafe/high-cardinality metric labels: {', '.join(unexpected)}")
    normalized: list[str] = []
    for key, value in sorted(labels.items()):
        text = str(value)
        if len(text) > MAX_LABEL_VALUE_LENGTH:
            raise ValueError(f"metric label {key!r} exceeds bounded cardinality length")
        if _is_sensitive_key(key):
            raise ValueError(f"sensitive metric label is forbidden: {key}")
        normalized.append(f"{key}={text}")
    return f"{normalized_name}{{{','.join(normalized)}}}"


class RuntimeMetrics:
    """In-process metrics registry with bounded, privacy-safe labels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._timers: dict[str, dict[str, float]] = {}
        self.started_at = time.time()

    def increment(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            self._counters[key] += amount

    def observe(
        self,
        name: str,
        duration_seconds: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            current = self._timers.setdefault(
                key,
                {"count": 0.0, "totalSeconds": 0.0, "maxSeconds": 0.0},
            )
            current["count"] += 1
            current["totalSeconds"] += duration_seconds
            current["maxSeconds"] = max(current["maxSeconds"], duration_seconds)

    @contextmanager
    def timer(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - started, labels=labels)

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


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    name: str
    status: HealthStatus
    required_for_readiness: bool = False
    required_for_autonomy: bool = False
    checked_at: float = field(default_factory=time.time)
    reason: str | None = None

    def public(self) -> dict[str, Any]:
        return sanitize_telemetry(
            {
                "name": self.name,
                "status": self.status.value,
                "requiredForReadiness": self.required_for_readiness,
                "requiredForAutonomy": self.required_for_autonomy,
                "checkedAt": self.checked_at,
                "reason": self.reason,
            }
        )


class RuntimeHealth:
    """Machine-readable liveness/readiness/degraded/autonomy health model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dependencies: dict[str, DependencyHealth] = {}
        self.started_at = time.time()

    def set_dependency(
        self,
        name: str,
        status: HealthStatus | str,
        *,
        required_for_readiness: bool = False,
        required_for_autonomy: bool = False,
        reason: str | None = None,
    ) -> DependencyHealth:
        dependency = DependencyHealth(
            name=str(name).strip(),
            status=HealthStatus(status),
            required_for_readiness=required_for_readiness,
            required_for_autonomy=required_for_autonomy,
            reason=reason,
        )
        if not dependency.name:
            raise ValueError("dependency name must not be empty")
        with self._lock:
            self._dependencies[dependency.name] = dependency
        return dependency

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            dependencies = list(self._dependencies.values())
        readiness_blocked = any(
            dep.required_for_readiness and dep.status in {HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN}
            for dep in dependencies
        )
        autonomy_blocked = any(
            dep.required_for_autonomy and dep.status != HealthStatus.HEALTHY
            for dep in dependencies
        )
        degraded = any(dep.status == HealthStatus.DEGRADED for dep in dependencies)
        unhealthy = any(dep.status == HealthStatus.UNHEALTHY for dep in dependencies)
        unknown = any(dep.status == HealthStatus.UNKNOWN for dep in dependencies)
        overall = (
            HealthStatus.UNHEALTHY
            if unhealthy
            else HealthStatus.DEGRADED
            if degraded
            else HealthStatus.UNKNOWN
            if unknown
            else HealthStatus.HEALTHY
        )
        return {
            "liveness": True,
            "readiness": not readiness_blocked,
            "status": overall.value,
            "degraded": degraded,
            "autonomousExecutionEligible": not autonomy_blocked and not readiness_blocked,
            "uptimeSeconds": max(0.0, time.time() - self.started_at),
            "dependencies": [dep.public() for dep in sorted(dependencies, key=lambda item: item.name)],
        }


@dataclass(frozen=True, slots=True)
class TraceSpan:
    span_id: str
    name: str
    started_at: float
    ended_at: float
    duration_seconds: float
    correlation_id: str
    causation_id: str | None
    parent_span_id: str | None
    status: str
    attributes: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return sanitize_telemetry(
            {
                "spanId": self.span_id,
                "name": self.name,
                "startedAt": self.started_at,
                "endedAt": self.ended_at,
                "durationSeconds": self.duration_seconds,
                "correlationId": self.correlation_id,
                "causationId": self.causation_id,
                "parentSpanId": self.parent_span_id,
                "status": self.status,
                "attributes": self.attributes,
            }
        )


class SpanExporter(Protocol):
    def export(self, span: TraceSpan) -> None: ...


_current_span_id: ContextVar[str | None] = ContextVar("codex_web_span_id", default=None)


class RuntimeTracer:
    """OpenTelemetry-compatible seam without forcing a concrete SDK dependency."""

    def __init__(self, exporter: SpanExporter | Callable[[TraceSpan], None] | None = None) -> None:
        self.exporter = exporter
        self._lock = threading.Lock()
        self._recent: deque[TraceSpan] = deque(maxlen=MAX_RECENT_SPANS)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[str]:
        context = current_correlation()
        if context is None:
            with correlated() as generated:
                with self.span(name, **attributes) as span_id:
                    yield span_id
                return
        span_id = uuid.uuid4().hex
        parent_span_id = _current_span_id.get()
        span_token = _current_span_id.set(span_id)
        started_at = time.time()
        status = "ok"
        try:
            yield span_id
        except Exception:
            status = "error"
            raise
        finally:
            ended_at = time.time()
            _current_span_id.reset(span_token)
            span = TraceSpan(
                span_id=span_id,
                name=str(name),
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=max(0.0, ended_at - started_at),
                correlation_id=context.correlation_id,
                causation_id=context.causation_id,
                parent_span_id=parent_span_id,
                status=status,
                attributes=sanitize_telemetry(attributes),
            )
            with self._lock:
                self._recent.append(span)
            if self.exporter is not None:
                if hasattr(self.exporter, "export"):
                    self.exporter.export(span)  # type: ignore[union-attr]
                else:
                    self.exporter(span)  # type: ignore[operator]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [span.public() for span in self._recent]


def install_observability(app: FastAPI, host: Any) -> RuntimeMetrics:
    existing = getattr(app.state, "runtime_metrics", None)
    if isinstance(existing, RuntimeMetrics):
        return existing

    configure_logging()
    metrics = RuntimeMetrics()
    health = RuntimeHealth()
    tracer = RuntimeTracer()
    log_store = _RUNTIME_LOG_STORE
    app.state.runtime_metrics = metrics
    app.state.runtime_health = health
    app.state.runtime_tracer = tracer
    app.state.runtime_log_store = log_store

    if hasattr(host.hub, "configure_observability"):
        host.hub.configure_observability(metrics)

    codex_runtime = getattr(app.state, "codex_runtime", None)
    if codex_runtime is not None:
        codex_runtime.metrics = metrics

    @app.middleware("http")
    async def correlate_http(request: Request, call_next: Any) -> Any:
        incoming_correlation = _bounded_identifier(request.headers.get(CORRELATION_HEADER))
        incoming_causation = _bounded_identifier(request.headers.get(CAUSATION_HEADER))
        identity_actor = getattr(request.state, "identity_actor", None)
        with correlated(
            correlation_id=incoming_correlation,
            causation_id=incoming_causation,
            tenant_id=getattr(identity_actor, "organization_id", None),
            workspace_id=getattr(identity_actor, "workspace_id", None),
        ) as context:
            started = time.monotonic()
            status_code = 500
            try:
                with tracer.span(
                    "http.request",
                    method=request.method,
                    operation="http",
                ):
                    response = await call_next(request)
                    status_code = int(response.status_code)
            except Exception:
                metrics.increment(
                    "http.requests",
                    labels={
                        "method": request.method,
                        "status_class": "5xx",
                        "result": "error",
                    },
                )
                raise
            finally:
                metrics.observe(
                    "http.request.duration",
                    time.monotonic() - started,
                    labels={"method": request.method, "operation": "http"},
                )
            metrics.increment(
                "http.requests",
                labels={
                    "method": request.method,
                    "status_class": f"{status_code // 100}xx",
                    "result": "error" if status_code >= 500 else "ok",
                },
            )
            response.headers[CORRELATION_HEADER] = context.correlation_id
            return response

    router = APIRouter()

    def require_observability_reader(request: Request) -> None:
        actor = request_actor(request)
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "observability:read" not in actor.service_scopes:
                raise HTTPException(
                    status_code=403,
                    detail="observability:read service scope required",
                )
            return
        try:
            IdentityService.require_admin(actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/api/metrics")
    async def runtime_metrics(request: Request) -> dict[str, Any]:
        require_observability_reader(request)
        return metrics.snapshot()

    @router.get("/api/health")
    async def runtime_health(request: Request) -> dict[str, Any]:
        require_observability_reader(request)
        return health.snapshot()

    @router.get("/api/traces/recent")
    async def recent_traces(request: Request) -> dict[str, Any]:
        require_observability_reader(request)
        items = tracer.snapshot()
        return {"items": items, "count": len(items)}

    @router.get("/api/logs")
    async def recent_logs(
        request: Request,
        window_seconds: float = 900.0,
        limit: int = 100,
        level: str | None = None,
        logger: str | None = None,
        event: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        work_item_ref: str | None = None,
        execution_id: str | None = None,
        action_intent_id: str | None = None,
    ) -> dict[str, Any]:
        require_observability_reader(request)
        actor = request_actor(request)
        try:
            items = log_store.query(
                tenant_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                window_seconds=window_seconds,
                limit=limit,
                level=level,
                logger=logger,
                event=event,
                correlation_id=correlation_id,
                causation_id=causation_id,
                work_item_ref=work_item_ref,
                execution_id=execution_id,
                action_intent_id=action_intent_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "items": items,
            "count": len(items),
            "classification": "internal",
            "retention": {
                "storage": "bounded_memory",
                "maxEntries": log_store.max_entries,
                "maxAgeSeconds": log_store.max_age_seconds,
            },
            "payloadPolicy": (
                "structured allowlist only; free-form messages, exceptions, "
                "prompts, tokens and secret values are not returned"
            ),
        }

    @router.get("/api/observability")
    async def observability_snapshot(request: Request) -> dict[str, Any]:
        require_observability_reader(request)
        spans = tracer.snapshot()
        return {
            "metrics": metrics.snapshot(),
            "health": health.snapshot(),
            "recentTraces": spans,
            "traceCount": len(spans),
            "structuredLogs": {
                "classification": "internal",
                "maxEntries": log_store.max_entries,
                "maxAgeSeconds": log_store.max_age_seconds,
                "maxQueryResults": MAX_LOG_QUERY_RESULTS,
            },
        }

    app.include_router(router)
    return metrics
