# Runtime supervision

## Handoff delivery and acknowledgement deadlines

New structured handoffs persist `delivery.status=awaiting_dispatch`. Their
recipient acknowledgement window begins only after a successful native turn
start returns its actual turn ID under the admitted recipient profile. The
existing execution service carries the original handoff `requested_at` through
durable queue admission, drain, replacement and continuity dispatch. An atomic
canonical update matches that nonce, recipient, tenant and Project, rejects
closed or already acknowledged work, and stores the actual Thread, turn and
execution IDs with `delivery.status=delivered` and `started_at`. Queue admission,
an active marker, failed startup and unknown startup outcomes supply no delivery
or acknowledgement evidence. Delivery does not change ownership or accept work.

Delivery recording also compares the successful assignment's actual repository
target and writable scope with the current canonical task under the same atomic
record lock. Matching role, tenant and nonce alone is insufficient. Repository
delivery requires the task's declared writable set and admitted primary; missing,
broader, wrong or changed repository scope leaves delivery unqualified and logs a
scope rejection. The existing broker coordination profile qualifies only with no
repository target or writable/read-only grant. Tenant and Project evidence comes
from the successful canonical assignment rather than the requested Project.
Verified starts persist the admitted profile and repository scope with
`scope_verified=true`, separately visible in the inspector. These optional fields
are additive; older delivered records remain explicitly unverified in scope and
retain their original timestamps. No retrospective scope or clock is invented.

Undelivered handoffs have a separate bounded dispatch deadline configured by
`CODEX_WEB_WORK_ITEM_HANDOFF_DELIVERY_TIMEOUT_SECONDS` (default 3600 seconds,
minimum 60). Once delivered, the existing acknowledgement timeout applies from
`started_at`. Expiry compares the complete current handoff atomically before
archiving it, preserves its real acknowledgement evidence, and replaces the
stale acknowledgement instruction with the sender's recovery action. Existing
watchdog cooldowns, active/queued exclusions, scoped dispatch and reasoning gates
remain authoritative; this change introduces no additional model polling.

Compatibility is additive: handoff `delivery` and queued-turn
`handoff_requested_at` are optional persisted fields. Historical missing delivery
remains untracked and keeps its existing request-time expiry; timestamps are not
retrofitted. Old queued entries without a nonce cannot establish delivery. New
readers preserve original queue IDs, order, messages, scope and policy attribution;
resaving may add null fields. Deployment qualification must compare every
original field while allowing those declared additions. An older writer can
discard delivery metadata, so rollback requires a canonical snapshot and drained
admissions rather than mixed-version writers. The work-item detail UI displays
delivery status and recipient start separately from handoff acceptance.

Long-lived process lifecycle is owned by `codex_web.services.runtime_supervisor.RuntimeSupervisor`. The supervisor is a composition root for lifecycle only; it does not implement autonomy, work-item continuity, or native recovery policy itself.

The five autonomy watchdog tasks remain dormant when their effective policy interval is zero, including startup with `AUTONOMY_DISABLED` present. They recheck the interval every five seconds without dispatching work, so removing a maintenance pause resumes the same tasks without another restart. Effective intervals are refreshed before each cycle; normal configured cycle delays still apply. Other explicitly disabled periodic loops retain their existing exit behavior.

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

Routine GitLab work remains with the canonical implementation/validation owner
or pending handoff recipient. Failed work selects its explicit next action owner
through the same actionable-owner rule as continuity dispatch. Orchestrator is
the exception path for inconsistent routing, missing ownership, or an explicit
coordination/recovery action. Repository attestation and publication reconciliation
remain with the admitted repository worker; a coordination-only profile cannot
gain repository authority from a recovery request.

Authenticated provider updates enter the canonical event journal before semantic
routing suppression. Semantic fingerprints include material source attributes,
head/commit and change facts; equal labels alone cannot suppress changed code or
issue content. Workflow-label-only feedback may be recorded without a model turn
only when an available same-tenant/Project pre/post snapshot proves business,
ownership, scope, handoff and evidence state unchanged, current labels agree with
canonical workflow state, and routing has no conflict. Unknown changes, missing
observations and actual deltas retain normal owner reasoning. Existing event/cycle
and dispatch observations expose the deterministic result without an extra model
or recovery wake; retained queue entries are not removed by this rule.

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

If interrupt returns the exact structured Codex error `-32600` / `no active
turn to interrupt`, the provider has established that the old turn is terminal.
Steering retains its fence and admits the same queued turn once, recording
`steering_turn_already_terminal`. A failed replacement start clears the stale
active marker and restores the same queued item and execution identity.
Transport failures, timeouts and other interrupt errors preserve the original
active marker and queue; they do not establish terminal evidence.

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
Outbound workflow-claim verification runs through `asyncio.to_thread`, preserving
the actor/tenant ContextVars and awaiting the existing findings/correction before
provider delivery and the next native notification. A cancelled wait does not
send or retry the update. Messages without qualified or bare issue references
do not read the Work Item catalog; qualified-reference matching and unique bare
number ambiguity checks retain their existing semantics. Referenced catalog
scans still exist and this off-loop isolation does not claim to remove their
project-size CPU cost.
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

Worker concurrency exhaustion is backpressure: queue drains preserve the same
message without consuming execution retries and retry admission after 30 seconds.
An interrupt timeout also preserves the fenced running session and requeues the
steering message, because cancellation may have succeeded without an observed
acknowledgement. Interrupted turns retain the canonical handoff checks.
Activity updates for an existing bot binding preserve its recovered thread ID.
The thread list renders pending queue depth alongside busy status and refreshes
that depth from canonical project binding snapshots.

Thread queue admission commits each validated task to the existing transactional
TurnQueueRepository before reporting success. `CODEX_WEB_MAX_THREAD_QUEUE_DEPTH`
is a scheduling pressure threshold (default 12), not permission to discard an
incoming task. Queue inspection and status events expose `queueHighWaterMark`,
`queueUnderPressure`, and `retainedBeyondHighWaterMark`; existing thread badges
show the complete retained depth. Crossing the threshold preserves FIFO,
execution identity, profile/repository scope and reply targets. It does not
increase worker concurrency or bypass execution admission. Storage errors
propagate and never establish successful retention. Duplicate inputs reuse
their existing retained item. No new work object or storage schema is introduced.

This protects thread-queue saturation. It does not establish crash durability
for raw Slack envelopes acknowledged into an in-memory ingress shard, or
crash-safe reconciliation of an unknown native start outcome. Those boundaries
remain distinct from successful durable queue admission.
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

Work-item startup and captured queue admission treat their requested read-only
repository context as authoritative, including an empty set. They must not fill
that set from an older thread's broad defaults. Ordinary unscoped startup retains
the existing thread-default behavior. Explicit required read contexts remain
unchanged and still pass canonical resource/profile authorization.

After the active-turn guard, scoped startup persists the requested context and
explicit primary/writable settings before bootstrap creation or replacement.
An idle bootstrap whose read-only context differs is superseded through the
existing authorized binding path, just like primary/writable/runtime drift.
Authority is a set; ordering alone does not trigger a replacement. This prevents
unrelated inherited read leases from blocking another repository's owner without
editing a live lease or changing the actor, profile revision or grant catalog.

A queued request pinned to a different revision of the same Agent Profile uses
canonical invocation/access routing to supersede its idle bootstrap with that
exact revision and original actor. Revision transitions may go in either direction;
a resolver returning a different revision is rejected before supersession. A
different collaborator identity remains an immutable-profile refusal.

The controller's `thread_agent_profile_immutable` refusal occurs before native
turn startup. An authorized preflight reconciliation action may reclassify a
historical uncertain attempt as blocked only when that stored structured refusal
matches the immutable request's profile ID/revision and records a different bound
revision of the same profile. It cannot clear a real start receipt, infer absence
from markers/timeouts/expiry, or automatically replay the request. Administrator
and MFA policy apply, and original-scope retry remains a separate explicit action.
The existing inspector exposes reconciliation only for this verified refusal.

Queued admission writes the complete original `QueuedTurn` into the existing
preflight recovery domain before removing it from the queue. The snapshot keeps
its ID, creation/order provenance, actor, exact profile revision, work-item and
handoff nonce, repository/policy scope and reply destination. Retention failure
leaves the queue intact. An atomic pre-start claim changes queued admission to
`admitting` before queue removal. A second claim-checked gate changes that state
to reconciliation-required before any native startup. Explicit recovery of an
interrupted pre-start claim fences its old token; that caller can no longer pass
the native-start gate. Concurrent drainers cannot acquire the same claim twice.
A durable reconciliation-required gate commits before
native startup; uncertain outcomes cannot be replayed through retry or steering.
An active marker or an expired retry claim does not establish successful start.
The native runtime records a claim-bound provider start receipt before bootstrap
and other post-start bookkeeping. It includes the requested execution, actual
assignment execution and native session/turn/runtime IDs. A bookkeeping failure
preserves actual active-turn ownership and cannot rewrite that receipt as an
unstarted failure. Only that receipt permits removing a redundant restored queue
copy. A request with no receipt remains uncertain; absence and timeout are not
proof of an unstarted request.

Interrupted explicit queue recovery is distinct from native admission: its
`retrying` state cannot pass the native-start gate. A stale recovery token may be
replaced while fencing the old admission and recovery tokens; this restores only
the original queue request, and ordinary admission still has to claim it. An
uncertain native admission cannot enter this recovery path.
Confirmed preflight rejection retains a blocked request for explicit recovery;
later eligible requests can proceed. Maintenance and capacity waits retain their
existing queue/backoff path. A missing Project cannot justify inventing tenant
provenance: its original entry remains retained while eligible entries are tried.

Administrator retry reconstructs the original queued request, preserving its
identity and authorization inputs. It re-enters ordinary queue admission rather
than substituting the administrator's profile or declaring delivery. Canonical
thread replacement may retarget the delivery Thread and reply Thread only;
the original snapshot remains immutable. The preflight inspector shows the
original queue ID, task/profile attribution and truthful admission/unknown state.

Early event deduplication compares the complete authorization and delivery
identity, including profile revision, actor, repository scope, reply destination
and handoff nonce. Equal message text alone cannot suppress a new handoff.
Retained-request matching excludes only the newly generated queue/execution IDs,
creation timestamp and retry count; the original snapshot remains unchanged.
A native start response without an actual turn ID produces an unknown-outcome
event and retained reconciliation state, never a successful-start receipt.

This extends the application-level persisted preflight payload with an optional
queued snapshot and new disposition values. Existing records remain readable by
the new application; older strict preflight readers cannot read new payloads.
There is no SQL DDL or retrospective start/ACK backfill. Rollout requires drained
admissions and a declared persisted-payload compatibility change. Mixed-version
writers are unsupported, and rollback requires the canonical pre-transition
snapshot under maintenance; source-only rollback cannot safely interpret new
preflight dispositions. The pre-transition snapshot supports fenced recovery
before new admissions resume. After new work runs, restoring that older snapshot
must not discard later requests or provider outcomes or replay their starts;
those require separate authoritative reconciliation. No compatible source
rollback is advertised for this transition. A reserved queue correlation
identifies intent, not a native execution receipt; legacy entries without an
execution ID use that declared correlation while their original snapshot stays
unchanged.

A Project readiness request with missing or expired trusted-local account
evidence performs a coalesced `account/read` metadata request and reevaluates
readiness. Project scope and authentication policy are checked first. Fresh
proof skips the read; failed or negative reads retain the authentication
blocker. This is request-driven, consumes no model tokens, and creates no idle
polling loop. Canonical readiness state reads and writes run off the event loop
so large work-item collections cannot delay the account RPC response reader.


## Durable thread replacement routing

Actual assignment-bound thread bootstrap replacements are recorded in the
canonical thread-index recovery state before bot bindings, queued work or
archival are retargeted. The versioned replacement graph retains each original
edge and the bootstrap Project, actor/tenant, profile revision and repository
scope provenance. Missing actor metadata remains explicit rather than inferred.
A transactional update rejects conflicting destinations, cycles and known
cross-tenant or cross-Project chains. Restarted recovery services resolve this
graph before consulting the process-local compatibility map.

A replacement link supplies routing, not execution authority or delivery proof.
Retained-request recovery still reconstructs its original actor, requested
profile revision and repository scope and passes the normal admission gates.
Existing preflight inspector replacement links expose the resolved destination;
no new operator action or permission grant is introduced. Historical routes
without durable edges require separate authoritative replacement evidence;
thread names and latest role bindings cannot establish them. An archived-session
refusal remains retained until its original preflight outcome is explicitly
reconciled; persistence does not authorize replay of an uncertain start.

The additive recovery document is ignored by older readers and does not mutate
existing queue or preflight schemas. Downgrading loses durable routing capability
and cannot be treated as a repair for retained work. Deployment and recovery
continue through the canonical upgrade and backup boundaries.
