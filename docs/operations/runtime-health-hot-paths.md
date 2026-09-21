# Runtime health hot paths

Runtime liveness, readiness, and status endpoints must remain bounded even when operational state is large.

## Execution model

Expensive runtime health evaluation is performed by one shared background refresh loop. Request handlers read the latest bounded snapshot; they do not trigger synchronous registry/journal scans.

Defaults:

```text
CODEX_WEB_RUNTIME_HEALTH_REFRESH_SECONDS=5
CODEX_WEB_RUNTIME_HEALTH_CACHE_MAX_AGE_SECONDS=20
CODEX_WEB_RUNTIME_SLOW_OPERATION_SECONDS=0.25
CODEX_WEB_EVENT_LOOP_LAG_WARNING_SECONDS=0.25
CODEX_WEB_SLOW_HTTP_REQUEST_SECONDS=1.0
```

Concurrent refresh callers coalesce onto the same in-flight evaluation. Cancelling one waiter does not cancel the shared refresh.

## Endpoint contract

- `/api/livez` is process/runtime liveness only and performs no large-state enumeration.
- `/api/readyz` consumes the latest cached runtime/state-store health summary.
- `/api/status` consumes cached active/queued counters and does not invoke Codex recovery or load complete active-turn/queue registries.
- `/api/operations` remains an operator diagnostic surface and its blocking snapshot work is dispatched off the HTTP event loop.
- full diagnostics remain governed by the bounded diagnostics contract.

A missing, expired, or failed health refresh is reported explicitly through `healthCache`; requests do not force an emergency synchronous refresh.

## Refresh profiling

Each expensive refresh operation records:

- elapsed seconds;
- record count when available;
- bytes read when available;
- stable refresh correlation ID;
- error class;
- slow-call count.

Current profiled operations include binding loading, turn-queue loading, bounded telemetry-tail reads, Slack health, execution readiness, GitLab health, active-turn counting, and StateStore status.

Slow operations emit `runtime_slow_operation` telemetry with the refresh ID. Event-loop lag is measured by the refresh scheduler and exposed as current/max lag.

## HTTP observability

The existing correlation/tracing middleware records bounded request spans with method and path. Requests slower than `CODEX_WEB_SLOW_HTTP_REQUEST_SECONDS` emit a structured `runtime.slow_http_request` event carrying the request correlation ID.

## Failure behavior

If a refresh fails, the last successful snapshot remains available for diagnostics, but readiness is unhealthy and reports the refresh error class. A stale cache also makes readiness unhealthy without executing unbounded fallback work.

Static asset version calculation (including Git revision lookup) is performed once during service composition. The request-time version accessor is memory-only.

## Large-state qualification

Regression coverage includes:

- slow filesystem/loader simulation without event-loop starvation;
- repeated cached reads with zero repeated expensive loader calls;
- concurrent refresh coalescing;
- cancelled waiter with refresh continuation;
- stale cache;
- malformed refresh state;
- slow-operation correlation metrics;
- cached static version access;
- correlated slow HTTP request spans.
