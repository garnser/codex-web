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

`RuntimeSupervisor` only starts, schedules, coordinates ownership leases, and stops those collaborators. Runtime behavior is supplied through explicit service dependencies; there is no legacy runtime host.

## Lifecycle

On startup the supervisor:

1. performs persisted-state housekeeping,
2. starts the Codex runtime,
3. authenticates one heartbeat for the process-owned local execution worker,
4. runs startup recovery when autonomy policy allows it,
5. starts provider/runtime owners and periodic cycles, including the local-worker heartbeat,
6. schedules one cooldown-protected native recovery pass.

Persisted stale-worker and expired-lease reconciliation runs before the
process-owned local worker is ensured. This ordering prevents slow application
composition from aging a newly written heartbeat and offlining the worker
during its own startup. The supervisor then heartbeats the idle local worker
without invoking a model, so a later stale-worker sweep cannot strand queued
work. Heartbeat failures are observable runtime events and do not terminate
unrelated supervised tasks.

On shutdown it:

1. marks native recovery as shutting down,
2. cancels supervisor-owned periodic/startup tasks,
3. stops provider owners,
4. releases replicated ownership leases,
5. cancels continuity and native-recovery tasks,
6. stops assignment-bound worker sessions, bot runtime, and Codex runtime.

Recovery scheduling is idempotent inside the configured cooldown window. Continuity checks capture the expected owner/handoff identity when scheduled and abort if the canonical work item changes before the check runs.

Owner-work supervision also treats canonical actionable ownership as a durable
wake condition. For each configured agent owner, an idle lane with no active or
queued turn receives at most one exact canonical Work Item per bounded dispatch
window. Active, queued, pending-handoff, closed, and non-actionable lanes are
skipped deterministically. Recent historical activity does not satisfy the
invariant because it does not prove that pursuit is ongoing. Runtime readiness
fails when autonomy is disabled or a required autonomy task has stopped, so an
idle autonomous deployment cannot report healthy while its supervision plane
is inactive.

### Active-turn restart recovery

An `ActiveThreadTurn` is durable evidence that a turn was in progress, not
proof that its process-local task survived an application restart. On startup,
local and single-node deployments therefore submit both fresh ownerless turns
and turns whose persisted local assignment still appears live to the bounded
resume path. Replicated deployments continue to respect shared assignment and
worker leases; one application instance must not infer that another instance's
live owner disappeared.

Before starting the replacement turn, recovery releases the interrupted
active-turn marker so the normal turn-start admission check does not reject its
own recovery attempt. A successful start installs a new canonical marker. If
start fails before that happens, recovery restores the interrupted marker with
its incremented attempt count and last-attempt timestamp. Startup retries are
bounded to three attempts and remain observable through the active-turn
recovery records and runtime events.

## Steering handoffs

When a durable Thread bootstrap outlives its historical execution assignment,
an unprefixed model cannot identify the former runtime. Turn admission resolves
an eligible runtime through canonical routing using the current invoking actor
and exact requested Agent Profile revision before creating a fresh assignment.
The recovered bootstrap retains the resolved profile binding. Routing denial
remains fail-closed; recovery never falls back to the ambient runtime or an
administrator identity.

Steering a durable queued turn into an active Thread is a fenced handoff. The
runtime retains the prior active-turn record until interrupt and replacement
start either commit or roll back. Observational `thread/read` timeouts and
terminal projections may report an error during that window, but they must not
retire the process generation or clear the fenced active record. A failed
interrupt or replacement start requeues the same `QueuedTurn` at the front with
its original metadata and stable execution identity. The API reports a typed,
retryable unavailable/conflict result and the UI refreshes canonical queue and
turn state before offering retry. Started, interrupted, requeued and resumed
outcomes remain observable as distinct runtime events.

The current handoff fence is process-local. Restoring the queue after a caught
exception and retaining an execution ID do not establish crash-safe delivery
or runtime-level at-most-once semantics for an unknown start outcome. Those
cases require durable claim/outcome reconciliation before the steering contract
can provide that guarantee. UI retry controls remain disabled until both queue
and Thread reads succeed and the exact queued item is still present; a failed
read is shown as reconciliation failure, not successful recovery.

## Compatibility boundary

`runtime.core` is an intentional, definition-free compatibility namespace for the verified historical `import server` surface. Application composition may publish aliases to canonical services onto that namespace, but production services never read behavior or state from it.

FastAPI, EventHub, runtime lifecycle supervision, diagnostics, provider runtimes, work-item continuity, and process startup all have explicit owners outside the compatibility namespace. New production code must depend on those owners directly rather than adding new aliases or mutable state to `runtime.core`.
