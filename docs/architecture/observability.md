# Observability, correlation, and service health

This document defines the canonical observability substrate for codex-web. Telemetry explains runtime behavior; it is never a second source of business state, authority, policy, or lifecycle truth.

## Correlation model

Every observable execution path should retain a stable **correlation ID** across boundaries and may retain a **causation ID** identifying the immediate triggering request/event. The runtime correlation context also has bounded fields for workspace, work item, execution, and ActionIntent identities as those domains become available.

Current boundaries:

- inbound HTTP accepts `X-Correlation-Id` and `X-Causation-Id`; invalid/oversized correlation values are discarded and a new ID is generated;
- responses echo the effective `X-Correlation-Id`;
- `EventHub.publish()` projects active correlation/causation and canonical scope identifiers into the event before listener/websocket fan-out;
- event listeners re-enter the event correlation context when a persisted/replayed event already carries a correlation ID;
- trace spans inherit the active correlation and parent span.

Future model-gateway, ActionIntent, worker, Artifact/Evidence, scheduler and provider boundaries must propagate these canonical fields rather than inventing local tracing identifiers.

Correlation fields are diagnostic/provenance metadata. Their presence does not authorize an action or make an event canonical by itself.

## Structured logging and privacy

`log_event()` emits stable event names and structured fields. `JsonFormatter` adds active correlation fields and sanitizes telemetry before serialization.

Telemetry sanitization redacts secret-bearing keys such as authorization headers, credentials, passwords, prompts, secrets and access/refresh/service tokens. Safe usage counters such as `input_tokens` and `output_tokens` remain measurable; raw bearer/API token material does not.

Do not emit prompt bodies, task/repository content, cookies, credentials, encryption keys, secret values, or provider response bodies merely to improve diagnostics. Later data-governance rules may further restrict export/retention of otherwise safe fields.

The built-in structured-log query path is intentionally stricter than stdout JSON logging. It stores at most 500 entries for at most one hour in process memory and returns only an allowlist of structured metadata plus correlation/object references. Free-form log messages, exception strings, arbitrary structured bodies, prompts and secret/token values are never returned by `/api/logs`. Tenant queries only return entries whose organization and workspace exactly match the authenticated operator; unscoped process logs are excluded from tenant queries.

This bounded buffer is classified as **internal runtime telemetry**. It is transient diagnostic data, not canonical business state or durable audit evidence. Longer retention/export belongs in a separately governed observability backend that preserves the same privacy and tenant-isolation rules.

## Metrics and cardinality

`RuntimeMetrics` is an in-process diagnostics registry. It supports counters and duration observations with a deliberately small allowlist of low-cardinality labels such as method, component, provider, result, status class, operation, queue, and kind.

Tenant/workspace/work-item/execution IDs are intentionally rejected as metric labels. Those identifiers belong in logs/traces where high cardinality is expected. Label values are length-bounded.

Telemetry exporters may translate these primitives to an OpenTelemetry-compatible backend later. Export must preserve the same privacy/cardinality rules.

## Tracing

`RuntimeTracer` provides a small provider-neutral span boundary with:

- correlation and causation IDs;
- nested parent span IDs;
- start/end/duration;
- status;
- sanitized attributes;
- a bounded in-memory recent-span buffer;
- an exporter seam compatible with an external tracing adapter.

The built-in buffer exists for local diagnosis and tests. It is not durable audit evidence.

## Health semantics

`RuntimeHealth` separates:

- **liveness** — the process is running;
- **readiness** — required dependencies are sufficiently healthy to serve normal work;
- **status** — healthy, degraded, unhealthy, or unknown;
- **autonomous execution eligibility** — dependencies required for autonomous execution are healthy.

Dependencies explicitly declare whether they are required for readiness and/or autonomy. A degraded optional dependency can keep the service ready while disabling autonomy. A required unhealthy/unknown dependency fails readiness and autonomy eligibility.

Health is deterministic application state derived from current dependency checks. It must not require an LLM.

## Operator APIs

The local runtime exposes:

- `GET /api/metrics` — bounded counters/timers;
- `GET /api/health` — machine-readable liveness/readiness/dependency/autonomy health;
- `GET /api/traces/recent` — bounded recent trace spans;
- `GET /api/logs` — bounded, tenant-scoped structured-log query with deterministic filters for level/logger/event/correlation/causation and canonical object references;
- `GET /api/observability` — combined diagnostics snapshot plus structured-log retention/query limits.

These endpoints are operational projections, not authority/state mutation APIs. The Platform Foundation UI (#141) can consume them without creating dashboard-local health truth.

## Extension requirements

New runtime/provider/model/worker/action code should:

1. enter or inherit an existing correlation context rather than generating unrelated IDs mid-flow;
2. use `log_event()` with stable event names for significant boundary/failure events;
3. use bounded metric labels only;
4. wrap external/model/worker calls in trace spans where useful;
5. register dependency health when that dependency affects readiness or autonomous execution;
6. keep secret/prompt/high-cardinality content out of metrics and sanitized from logs/traces;
7. link durable operational/audit evidence to canonical domain objects separately from transient telemetry.

Telemetry failure must not silently mutate canonical state, and telemetry availability alone must never grant authority or mark external actions successful.
