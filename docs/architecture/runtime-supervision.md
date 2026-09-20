# Runtime supervision

Long-lived process lifecycle is owned by `codex_web.services.runtime_supervisor.RuntimeSupervisor`. The supervisor is a composition root for lifecycle only; it does not implement autonomy, work-item continuity, or native recovery policy itself.

## Explicit owners

Application composition supplies the supervisor with focused collaborators:

- `RuntimePolicy` owns autonomy enablement, watchdog intervals, continuity delays, and native-recovery cooldown configuration.
- `AutonomyService` owns the owner-work, release-gate, work-item SLA, orchestrator, and split-brain cycles.
- `NativeRecoveryService` owns idempotent recovery scheduling, cooldown state, and recovery task cancellation.
- `WorkItemContinuityService` owns structured-handoff and actionable-owner dispatch/continuity tasks.
- `GitLabService` owns Support ServiceDesk sweep behavior and GitLab credentials/configuration.
- the composed Codex runtime, bot runtime, queue/recovery services, scheduler, and event-transport runtime own their respective execution behavior.

`RuntimeSupervisor` only starts, schedules, coordinates ownership leases, and stops those collaborators. It no longer discovers autonomy/watchdog/Support ServiceDesk behavior from `runtime.legacy_core`.

## Lifecycle

On startup the supervisor:

1. performs persisted-state housekeeping,
2. starts the Codex runtime,
3. runs startup recovery when autonomy policy allows it,
4. starts provider/runtime owners and periodic cycles,
5. schedules one cooldown-protected native recovery pass.

On shutdown it:

1. marks native recovery as shutting down,
2. cancels supervisor-owned periodic/startup tasks,
3. stops provider owners,
4. releases replicated ownership leases,
5. cancels continuity and native-recovery tasks,
6. stops assignment-bound worker sessions, bot runtime, and Codex runtime.

Recovery scheduling is idempotent inside the configured cooldown window. Continuity checks capture the expected owner/handoff identity when scheduled and abort if the canonical work item changes before the check runs.

## Compatibility boundary

Historical names written onto `runtime.core` are output-only compatibility aliases. They delegate into the extracted services and exist for direct imports/tests during the `legacy_core` deletion sequence. New runtime behavior must depend on explicit services and must not add state or behavior back to `runtime.legacy_core`.

Legacy watchdog task-handle globals may still be mirrored for diagnostics until the runtime diagnostics/global extraction lands. They are not the owners of the tasks.
