# Runtime supervision

Long-lived process lifecycle is owned by `codex_web.services.runtime_supervisor.RuntimeSupervisor`.

The supervisor replaces the legacy `runtime.core` startup and shutdown event handlers during application composition. It owns:

- systemd watchdog notifications
- Support ServiceDesk sweep scheduling
- owner-work, release-gate, work-item SLA, orchestrator, and split-brain watchdog scheduling
- queue-recovery scheduling
- startup recovery/name-restoration tasks
- coordinated cancellation of continuity tasks
- Slack provider, bot runtime, and Codex runtime shutdown ordering

The legacy task globals in `runtime.core` are populated with the supervisor-owned task handles for compatibility with existing diagnostics. New long-lived background tasks should be registered with the supervisor rather than added directly to `runtime.core`.

## Health and diagnostics ownership

Operator-facing health and diagnostics are composed from explicit services rather
than reading implementation helpers from `runtime.legacy_core`:

- `StaticAssetVersionService` owns cache-busting versions for UI assets.
- `BotRuntimeTelemetry` owns the provider event journal, recent-event reads,
  thread activity ages, and recent dispatch counts.
- `RuntimeHealthService` combines Codex readiness, provider-runtime task
  health, queue age, terminal failure state, delivery failures, Slack backfill
  health, and GitLab synchronization health without exposing credentials.
- `RuntimeDiagnosticsService` owns the authenticated diagnostics projection,
  work-item summary counts, public connection/binding projections, queue/task
  state, and project filtering.
- `BotRoutingService.preview` is the canonical, side-effect-free route
  diagnostic. It never creates or clones a binding.

The `api/ui.py` and `api/system.py` routers receive those services directly.
The diagnostics endpoint keeps its existing admin/service-scope authorization
boundary, and connection data is projected through `BotConnectionService.public`
so raw credential values are never included.

Historical helper names on the compatibility runtime may temporarily point to
these extracted owners, but production routers must not resolve diagnostics
behavior through the compatibility host.

