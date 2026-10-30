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

### Home checkpoint component timings

The existing Home checkpoint boundary emits `home.checkpoint.stage` events
through `operation_stage()`. A request-local scope enables fixed component names
for current selection status, Profile resolution, Goal inspection, custody,
Session lookup, held-action observation, each of the three preservation
snapshots, and the Artifact writer. Scope is reset on every return or refusal;
ordinary Home/Profile reads outside the checkpoint do not emit these events.

Events contain only the inherited correlation, tenant/workspace, fixed stage,
completed/refused outcome and elapsed seconds. They omit content, exception text,
stacks and Artifact/receipt payloads. Durations are inclusive wall time, so nested
stages must not be added together or presented as SQL/CPU measurements. A refused
final snapshot can follow a committed preservation Artifact; timing telemetry
cannot establish absence of effects or authorize a retry. Existing inspectors
use the same bounded log query surface; no new control or state projection is
introduced. The five-second preservation budget and all fresh authority,
selection, process and action observations remain unchanged.

### Executive WorkGraph read component timings

Only the existing Executive WorkGraph read enables `work_graph.read.stage`
through `operation_stage()`. Its request-local scope follows the joined graph
reader and resets on return or refusal. Fixed stages separate the snapshot,
item headers, graph edges, repository initialization, PostgreSQL connection
acquisition, collection check, page execute/fetch and Python page decode.
Repeated page events expose call counts without page IDs, cursors, namespaces,
SQL, record content or exception text. Other Graph and storage callers do not
activate these timings.

These are inclusive wall durations, including scheduling and driver work;
execute/fetch time is not database CPU or an EXPLAIN measurement. The original
connection context manager receives the same exception and retains its exit and
suppression behavior. Queries, ordering, complete pagination, legacy conversion
fallback, savepoints, deadlines, authorization and retrieval audits do not
change. Timing observations neither establish an Executive deadline fix nor
permit a retry or context omission. Existing bounded log inspection displays
these events; no new operator controls or domain state are introduced.

### Bounded structured-log query buffer

The built-in runtime keeps a bounded in-memory structured-log projection for operator drill-down. It is intentionally narrower than stdout/stderr logging:

- only sanitized structured fields plus correlation/object references are retained;
- free-form log messages and exception text are **not** retained in the query buffer because they cannot be proven secret/prompt-safe from field names alone;
- entries are classified as `internal` telemetry and expire after one hour;
- the buffer holds at most 500 recent entries per process;
- queries are scoped to the caller's exact organization/workspace and exclude tenant-less or foreign-tenant entries;
- query windows are capped at 24 hours, results at 100 rows, and serialized response payload at 64 KiB;
- level, logger, event, correlation/causation, Work Item, execution and ActionIntent filters are exact deterministic filters;
- the buffer is local to one process/instance and is not durable audit evidence or canonical business state.

Longer-lived/centralized log storage may be added behind an exporter/query adapter later, but it must preserve the same tenant, classification, retention, redaction and bounded-query invariants instead of exposing raw process logs to the browser.

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

Catalog-backed health checks must use keyed or bounded paginated reads and
validate only the fields that determine the health result. They must not
reconstruct, deep-copy, or fully validate unrelated canonical payloads merely
to produce a diagnostic projection. Corruption of a field that influences the
health decision fails the refresh visibly; the projection never repairs or
mutates canonical state.

A canonical Project reference that no longer resolves is explicit admission
UNKNOWN (`project_not_found`), never a known hold or a default Project. That
observation preserves readiness failure while allowing the other health probes
to publish a fresh snapshot. Missing queue Project IDs and actionable-owner
Project references remain visible in the existing Operations projection; no
binding/activity lookup or canonical repair is attempted for a missing Project.
Unexpected storage, permission and validation errors still fail the refresh.

## Runtime diagnostics ownership

Process/operator diagnostics are composed from focused services rather than from the legacy runtime module:

- `RuntimeHealthService` derives daemon/provider health from Codex, bot runtime, queue, recovery, Slack and GitLab status collaborators.
- `RuntimeDiagnosticsService` builds the privileged process-wide snapshot and receives public serializers explicitly; raw provider credentials are never serialized directly.
- `StaticAssetVersionService` owns UI cache-busting version calculation.
- `OperatorUiService` owns the data projection for `/`, `/devstatus`, `/devhealth`, and the websocket presentation boundary.
- `BotRuntimeTelemetry` owns recent bot event journal reads, while `BotRoutingService` owns route-preview diagnostics.

The system and UI routers depend on these services directly. Compatibility names on `runtime.core` are temporary aliases only and must not become new implementation seams.

## Operator APIs

The local runtime exposes:

- `GET /api/metrics` — bounded counters/timers;
- `GET /api/health` — machine-readable liveness/readiness/dependency/autonomy health;
- `GET /api/traces/recent` — bounded recent trace spans;
- `GET /api/logs/recent` — bounded, tenant-scoped, redacted structured-log telemetry;
- `GET /api/observability` — combined diagnostics snapshot.

These endpoints are operational projections, not authority/state mutation APIs. The platform administration UI can consume them without creating dashboard-local health truth.

## Guarded read-only verification stages

Verification of `code-host.pull-request.observe-readiness.v2` emits the fixed
`guarded_readonly.verify.stage` event through the existing `operation_stage`
logger. Its request-local scope binds the exact canonical ActionIntent,
execution, correlation, tenant, workspace and optional Work reference, including
an absent Work reference. The scope follows ordinary `asyncio.to_thread`
propagation and resets on return, refusal or cancellation. Execute, legacy
readiness and unrelated operations do not enable this scope.

The fixed stages are `verification`, `provider_verification`, `admission`,
`inputs`, `custody` and `http`. Durations are inclusive wall time; nested durations
must not be added together. A completed stage means its body returned normally,
not that the canonical verification passed. Refused stages include exceptions
and cancellation, including partial time inside the interrupted operation. The
HTTP stage surrounds the existing reader-budget request boundary, including its
budget checks; it is not proof that a wire request started. Actual client time
continues to be charged by the unchanged cumulative network budget. Logging runs
after that charge and never supplies authority, changes deadlines or replaces
the original result or exception.

Operators can filter the existing tenant-scoped `GET /api/logs/recent` by
`event=guarded_readonly.verify.stage` and the exact `action_intent_id`, with
`limit=100`. The process-local buffer retains at most 500 entries for one hour;
queries and response sizes remain bounded by the existing API. These are partial
operational observations, not durable audit evidence: eviction, another process
or an unavailable log cannot establish that a stage or HTTP request never ran.
No provider URL, request/response content, raw exception, SQL, secret or native
output is added. Known execution receipts and canonical verification outcomes
remain the source of truth even when telemetry is unavailable.

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
