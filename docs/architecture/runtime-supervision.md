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

Event dispatch resolves the recipient's published execution profile before
starting or queueing work. An event's repository references do not grant
repository authority: profiles with no repository access receive no mutable
repository target or repository scope. The canonical work-item reference remains
attached for coordination and audit. Repository-capable recipients retain the
event's requested scope, subject to normal execution admission checks.

Recovery serializes replacements for each provider, project and logical agent;
turn admission serializes starts for each Thread. Slow provider calls for one
owner or Thread do not hold unrelated owners or Threads. Canonical assignment,
workspace, lease and fence checks remain responsible for resource authority.
Blocking profile and Skill lookups run outside the event loop so they do not
delay Slack keepalives or worker supervision.

On shutdown it:

1. marks native recovery as shutting down,
2. cancels supervisor-owned periodic/startup tasks,
3. stops provider owners,
4. releases replicated ownership leases,
5. cancels continuity and native-recovery tasks,
6. stops assignment-bound worker sessions, bot runtime, and Codex runtime.

Existing-thread Codex bootstrap rebinding can bypass the normal routing quota
probe. After superseded-session cleanup, it refreshes expired trusted-local
account evidence with one real `account/read` before canonical bootstrap
preflight. Fresh evidence and credential-backed or other runtimes do not add that
RPC. When repository rebinding omits the authentication mode, the prior
canonical assignment supplies it; an unknown mode never assumes operator
authentication. A failed read prevents bootstrap preparation; returned
account evidence still passes the existing authentication, actor, repository,
worker and transport checks. This metadata read does not invoke a model or
fabricate a session-availability result.

Recovery scheduling is idempotent inside the configured cooldown window. Continuity checks capture the expected owner/handoff identity when scheduled and abort if the canonical work item changes before the check runs.

Assignment-triggered owner steering reuses the same continuity owner rather
than adding a second Thread mutation path. The broker supplies exact
owner/Thread preconditions; continuity checks canonical actionable state,
Project binding, active/queued state, and its stable dispatch key before using
the normal event-dispatch seam. The dispatched turn retains the Work Item ref
and singular canonical repository scope. The event seam resolves the recipient's
published profile and removes repository scope for scratch-only profiles, so a
wakeup cannot promote execution authority.

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

## Configured project backlog delivery

A Project may explicitly bind delivery supervision to an existing same-Project
Thread. The canonical Project configuration retains the enabling identity and a
bounded scan interval. Administrators configure or disable it through the
TaskSource editor and `/api/projects/{project_id}/delivery-supervision`. Every
scan re-evaluates current membership and Thread ownership; stored configuration
cannot transfer authority to a different tenant or Project.

The supervisor discovers the configured authoritative TaskSource without model
reasoning, reconciles retained open items missing from fresh discovery through
provider reads (at most eight per scan), and selects canonical actionable work.
Closed work, explicit blockers, failed lanes, and pending handoffs are excluded.
An externally blocked release item does not suppress independently actionable
work. Active and queued delivery Threads are not awakened again. Global autonomy
pause, simulation, dry-run, and scoped autonomy policy remain canonical gates.

When actual delivery requires reasoning, a tenant-scoped canonical event enters
the bounded autonomy controller. A durable turn is queued before runtime startup,
so slow provider startup cannot consume the operator's request or lose the work
on restart. Event identity includes a bounded scan window: a failed dispatch is
retryable on a later scan without repeatedly sampling an idle or empty backlog.
Discovery and scan outcomes use existing runtime telemetry; the configuration
API also reports process-local last scan and next scan time, explicitly distinct
from durable Project configuration. There is no idle LLM polling.

## Codex transport and notification processing

Optional quota-read timeouts report unavailable measurements without retiring an
initialized authenticated Codex transport. Transport failure and required RPC
failure retain their existing recovery behavior. Definition reads for a known
record or definition validate only matching canonical revisions; publication,
checksums, scope selection, and security policy evaluation remain required.
Runtime usage accounting ignores display-only text/output chunks, so streaming
text does not create accounting writes that delay subsequent RPC responses.
Usage, tool lifecycle, and terminal events retain canonical attribution.
Assignment model connection attempts use a private bounded blocking executor
for authority checks and DNS, with a fifteen-second connection budget.

Canonical thread replacement is reconciled into the persisted delivery binding after rechecking the replacement thread’s Project and tenant scope. Delivery supervision therefore follows governed stale-thread recovery instead of repeatedly dispatching to a retired thread.
Codex stdout dispatch resolves RPC responses independently of its bounded,
ordered notification queue. Notification processing preserves native order and
existing projection, approval, and delivery checks. Runtime shutdown cancels
both readers and the notification processor.
Assignment process startup and teardown serialize per canonical assignment;
initializing an unrelated worker does not hold a runtime-wide startup lock.
Streaming text/output refreshes durable activity at most once every five seconds;
lifecycle events update immediately. This retains liveness evidence without one
state transaction per token. Steering interrupts include the canonical native
turn ID and may replace the active marker only during an owned steering handoff.
Display-only notification chunks coalesce only when adjacent native method and
item/turn/thread metadata match, preserving all text and lifecycle ordering.
Observers apply native-message filters before worker scheduling; browser fan-out
retains the stream. Usage projection reads its canonical record by ID instead of
loading the complete usage history for each measurement.

### Retained retry after stale-thread replacement

A retained preflight attempt keeps its original thread, request, execution ID,
Agent Profile revision and actor/tenant identity. Its optional replacement target
records only a canonical recovery mapping, qualified under the current retry
claim. Retry uses the ordinary admission path on that target, including repository,
authentication, policy and capacity checks. A stale replacement response alone is
not delivery: a web replacement bootstraps the exact admitted Agent Profile
revision and actor before retry may continue once on the verified replacement and then stops
with a retained failure if another replacement is needed. Started state still
requires actual execution admission. Both original and current thread views can
inspect the same attempt; the retry card displays original/current provenance and
loads the actual target on success or a new preflight denial. Historical target
index entries never authorize a different retained identity.

A Project readiness request with missing or expired trusted-local account
evidence performs a coalesced `account/read` metadata request and reevaluates
readiness. Project scope and authentication policy are checked first. Fresh
proof skips the read; failed or negative reads retain the authentication
blocker. This is request-driven, consumes no model tokens, and creates no idle
polling loop. Canonical readiness state reads and writes run off the event loop
so large work-item collections cannot delay the account RPC response reader.
