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
