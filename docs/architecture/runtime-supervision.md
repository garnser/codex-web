# Runtime supervision

## Thread runner inspection

Thread list/detail responses may include the additive `runner` projection
(`schemaVersion: "1.0"`) of the current bootstrap assignment. This read-only
projection decodes one tenant-filtered binding snapshot per list page, then
uses page-bounded indexed assignment/worker/current-turn reads and the current
fenced lease; it neither starts a session nor changes a turn, queue, assignment or
business outcome. Cancelled/failed/lost assignments and missing, expired or
mismatched leases cannot prove live custody. Missing or inaccessible evidence is
explicitly unknown, with cross-scope identity redacted.

Runner custody is separate from transcript state. Retained `inProgress` turns,
including synthetic bootstrap turns, keep their IDs/content/status for evidence
and reconciliation; they do not independently prove an active runner. Browser
list/detail indicators consume the same projection. Snapshots compare assignment
identity, creation/update time and lease fence; late activity events cannot
reactivate custody already observed as inactive.
`GET /api/threads/{thread_id}/runner` uses the same authenticated Thread/Project
scope dependency and reads custody and the existing exact current-turn marker
without provider/session startup. Its additive `activity` projection identifies
the current turn only when marker assignment and fence match. Live persistent
custody alone never sets Thread status or busy state: native idle/completed
Threads stay idle, while historical turns retain their original IDs/status.
Late unversioned events trigger refresh instead of replacing current activity. Assignment
execution/workspace/Project identity, lease fence/worker/expiry/deadline and trusted
worker lifecycle must match before reporting live. Queued work remains the
separate canonical durable queue projection; unavailable evidence stays unknown.

For Threads with a runner projection, unversioned provider activity only triggers
a coalesced canonical read (one pending request per Thread, five-second delay, ten-second read timeout).
The visible selected Thread also refreshes every 30 seconds, including waiting
and unknown states, so server lease renewal and new fences can permit genuine current-turn activity.
Renewal alone leaves an idle Thread idle.
Hidden tabs skip that timer; there are no model calls or admission/replay effects.
Failed refreshes clear busy indicators, expose uncertainty and retain ordering
watermarks. Unknown detail hydrates activity before the unloaded early return,
preserving the transcript and its unfinished turn IDs/status.
Browser caches are display state only; server admission remains authoritative.
Sidebar and selected-thread header share the same status presentation. Solid
accent styling and a filled glyph indicate active work only with a fresh live
process/lease projection and the canonical exact current-turn activity marker.
Live idle custody has an outlined idle glyph; waiting/queued/recovering and
stopped/lost/failed/unknown retain distinct text/glyphs. Existing theme tokens
provide contrast in light/dark and selected/unselected rows. Color never replaces
the label, diagnostic uncertainty, or provenance. After the earlier of lease
expiry and 60 seconds since observation (two existing refresh intervals), local
presentation drops active styling without initiating a read, turn, or replay.
This display freshness limit grants no authority or lease extension.
Lost assignments are explicitly `lost`; a pending assignment with a previous
fence is `recovering`. Neither establishes activity. A same-instance process
handle for the exact assignment/worker/fence can establish that its process has
stopped even while a last observed canonical lease remains valid. That read
reports `stopped`, diagnostic persistence state and outstanding executor work;
it never updates canonical assignment state. An old process cannot mask a new
claim. Historical unfinished turns remain evidence, not successful validation.
Runtime naming failures retain the canonical name and display scoped feedback.

## Bounded assignment supervision and failure custody

Native resume qualification reads the current process generation's initialize
response. The pinned CLI reports the configured `codex_web_local` client name
in its `userAgent`; that prefix and the legacy `codex_cli_rs` prefix qualify only
the exact `0.160.0` version. Other clients, versions, suffixes and missing or
malformed responses remain unqualified. External version probes and earlier
process generations cannot substitute for this handshake.

Each admitted process session owns one serial supervision executor and one
bounded diagnostic executor. These are lifecycle resources, not admission or
scheduling authorities. The watchdog validates worker/assignment/delegation and
resource bounds before renewal. Each validation and renewal wait is bounded by
the lesser of ten seconds and the remaining last validated lease. There is no
lease extension for delayed validation and no cached resource-limit bypass.
Default-executor browser projections cannot occupy these supervision slots.
Workspace and lease point reads use canonical committed records without the
compatibility workspace catalog writer mutex. An unrelated workspace creation or
cleanup must not block resource supervision behind catalog serialization. Legacy
record migration remains serialized and rechecks its state after taking that
mutex; point reads retain schema validation and service tenant/owner checks.
The disk cache mutex does not cover workspace metadata I/O. After resolving
metadata, a validator rechecks the complete cache under the mutex before scanning
files; a concurrent completed measurement wins over an older pending lookup.
Cache freshness starts before both observations. Expired observations and
cancelled or incomplete scans cannot publish a complete cache entry. The cache
interval, resource limits, authority checks and supervision deadline are unchanged.
Queue, validation/identity/resource, renewal and total wait timings distinguish
executor admission delay from work duration; they do not diagnose historical
CPU, GIL, OOM or database corruption without evidence.

Cancellation propagates a cooperative operation budget through existing store
locks, SQLite busy/progress/commit checks, PostgreSQL pool/statement/commit
checks and resource traversal. Start, completion and renewal use the existing
cross-record transaction primitive to read the worker and assignment and write
transition events together. Lease and assignment-deadline time is sampled after
the database writer lock is acquired. A delayed callback cannot borrow an old
clock or a later claimant's token/fence. Claim and cancellation timestamp reads
also occur inside their existing callbacks; they are not execution completion.

On supervision failure, fixed diagnostic metadata is captured before teardown.
The owned process group is killed independently of storage and executors,
including descendants of an already-exited parent. Process polling and runtime
transport cleanup are bounded separately. Only then is one one-second diagnostic
attempt made through canonical Evidence and worker events. Metadata includes
assignment/worker/execution/workspace/resource provenance, observed fence,
observed lease expiry, process ID/exit code and measured phase timings. Raw
exception/log bodies, credentials and assignment tokens are excluded; Evidence
is denied model context and uses canonical governance/retention. Failed runtime
Evidence is not successful test Evidence.

The assignment's control-plane broker registers accepted client transports
before scheduling their handlers. Stop closes its listener and aborts those
transports before waiting for server closure, so incomplete input or an unread
response cannot retain the worker's teardown and admission locks. Requests still
awaiting assignment validation, body input or actor resolution refuse dispatch
after stop. An operation already dispatched retains its existing canonical
ActionIntent execution and reconciliation ownership; transport shutdown neither
cancels that operation nor proves its outcome or authorizes replay. No lease,
fence, validation deadline, resource bound or authority is extended.

An original worker may append an observation only with retained original claim
provenance. The observation explicitly separates observed and current fences and
does not assert completion. A failed completion still requires the exact original
live lease, worker identity and fence in the canonical transaction. Expired or
reclaimed execution remains lost/recovering/current; diagnostics never resurrect
it. Failure release atomically compares the original failed assignment, worker,
fence and workspace lease before releasing ownership, always retaining files.
It cannot release a later claimant's workspace. Uncommitted Git source remains
in that retained workspace; ignored files are not silently discarded.

Before native launch, an available content service registers a canonical REPORT
Artifact for the exact assignment/fence and creates a private managed-output
slot inside the retained workspace. The trusted standard-library runner is a
read-only worker mount, exposed by `CODEX_WEB_MANAGED_VALIDATION_RUNNER`; the
registered slot is exposed by `CODEX_WEB_MANAGED_VALIDATION_DIR`. Existing native
assignment instructions require validation to invoke:
`python "$CODEX_WEB_MANAGED_VALIDATION_RUNNER" --source path/to/source -- command args`.
Repeated `--source` arguments identify up to 32 bounded source files. Before
launching the command in the same process group/sandbox/limits, the runner
atomically writes a versioned report containing source SHA256s, a command digest,
start time and unknown outcome. It records an exit observation only after actual
wait returns. Neither exit zero nor a worker-controlled report proves tests pass.

After process-group retirement, normal stop and failure retirement automatically
capture only registered bounded JSON reports through existing governed
ArtifactContent and informational TEST_RESULT Evidence. Source/report attribution
includes the registration Artifact, assignment, original worker and fence. There
is no caller pre-upload or arbitrary `/tmp` discovery. Reports reject extra
fields, log bodies, symlinks and oversized payloads; outputs are denied model
context. Interrupted reports retain `running`, null exit/finish and result
`unknown`. Existing Artifact/Evidence views expose their content and provenance;
the runner projection displays registration/capture/unavailability separately
from live custody. Repeated/concurrent stop callers share one owned cleanup task
and capture attempt, including cancellation of an awaiting caller. A retry after
partial persistence completes the linked INFO Evidence before reporting captured;
existing immutable content and exact-provenance legacy Evidence are reused. New
Evidence settlement uses canonical idempotency, and changed report content or
conflicting assignment/fence observations refuse without reinterpreting the
original observation. Broker cleanup
still runs when another cleanup component fails.

Unregistered commands and namespace-local `/tmp` files have no custody guarantee.
Missing content service or unavailable capture remains explicitly unavailable;
registration alone is not captured content. A process-wide crash before capture
leaves the registered report/source in the retained workspace for scoped recovery,
not a fabricated managed-content receipt. Independent validation/release gates
remain required. External unknown outcomes remain reconciliation only; no tool-log
parsing infers a passed test or replays an external action.

An additive `failureObservation` accompanies the exact same-instance runner
assignment/worker/fence. It retains the fixed failure code/phase, measured waits,
cleanup and process exit observations, original requester/tenant/Project/resource
and profile/Definition references, and source-validation custody availability.
It contains no lease tokens, raw exceptions, commands, environment or credentials.
The same metadata is emitted through existing scoped structured logging immediately
after process cleanup and after the bounded diagnostic attempt, including when
submission or persistence is unavailable. Logger failure cannot prevent cleanup
or the canonical diagnostic attempt. Logs are observations, never receipts or a
replacement authority. A process-wide crash may lose local status; journal and
retained canonical Evidence/Artifacts remain the recovery surfaces where available.

The runner route rechecks its authorized exact Project after asynchronous reads.
The existing Thread metadata UI displays failure phase/code, timing, cleanup,
diagnostic availability and a reconciliation next action, with scoped assignment,
Evidence (only if recorded) and provider-receipt inspection links. Unfinished work
has an unknown outcome from this observation; independently verified provider
receipts retain their canonical outcomes. There is no restart/replay action or
inference from an observation timeout. Historical `inProgress` content is retained.

Unavailable storage cannot be advertised as recorded Evidence. The local process
projection reports diagnostic state `unavailable` and outstanding executor work;
canonical expiry/recovery remains authoritative after process loss. Running
executor futures remain tracked until their cooperative exit. Arbitrary kernel
I/O or injected adapters cannot be forcibly interrupted by Python cancellation;
the deadline fences subsequent canonical writes, and the runtime must not claim
that such work has ended. Controlled lock faults must release and verify those
threads exit. Backend/kernel unresponsiveness requiring operator intervention
is a visible limitation, not an extra host grant.

Compatibility is additive: session/runner projections and worker event details
add metadata; persisted assignment/API/worker contract versions and Definition
revisions are unchanged. No mutable operational catalog, new storage domain,
policy, worker capability or authority is introduced. Existing drain, upgrade,
unknown-start and source custody contracts remain in force. Older displays may
show unfamiliar runner states as unknown; they cannot acquire execution authority.

## Handoff delivery and acknowledgement deadlines

New structured handoffs persist `delivery.status=awaiting_dispatch`. Their
recipient acknowledgement window begins only after a successful native turn
start returns its actual turn ID under the admitted recipient profile. The
existing execution service carries the original handoff `requested_at` through
durable queue admission, drain, steering, replacement and continuity dispatch. An atomic
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
copy. Without a receipt or exactly captured positive evidence, a request remains uncertain; absence and timeout are not
proof of an unstarted request.

### Exactly attributed positive native responses

Assignment-bound Codex queued originals retain `queued_start_correlation` before
the native `turn/start` RPC is written. Version `1.0` pins the immutable original
snapshot hash, actor/profile revision and requested repository scope,
tenant/Project, resolved actor, assignment execution/resources, worker and
send-time fence, native session, process generation and unique RPC ID. These are
metadata in the existing preflight record, not another mailbox or a grant. The
session supplies the validated assignment; task text and caller-supplied provider
logs cannot supply attribution.

Preparation is awaited storage work. After it finishes, the native write lock
checks the process generation and synchronously reuses the session's canonical
current worker/identity/delegation/assignment/lease guard. It compares the
send-time assignment/profile/resource binding and exact retained admission claim
again immediately before writing, with no intervening await. Changed authority,
fence, profile, claim or scheduling disposition prevents the write. Correlation
alone remains unknown even if this guard or a partial transport write fails;
exceptions and missing activity do not establish nonstart.

Only the trusted RPC reader captures the matching positive response under that
generation and request ID. It validates the result/turn shape, nonempty bounded
turn ID and every supplied session/thread identifier. Error, malformed,
mismatched and unassociated responses do not establish starts. Stored
`queued_start_evidence` version `1.0` contains only allowlisted native IDs,
generation/request attribution and observation time. No raw result, input,
credential, lease token or secret material is copied into evidence. A plain
`turn/started` notification, ordinary session read, equal message or close
timestamp supplies no original-request association and remains unknown.

Positive evidence commits before receipt projection. Each step atomically checks
the exact original and correlation; repeated/concurrent observations are
idempotent and conflicting receipts fail closed. The existing administrator/MFA
`reconcile-prestart` inspector action can finish a crash-interrupted positive
receipt projection from stored evidence. It accepts no proof or scope in its
body and makes no provider call. Its historical name also continues to support
the separately proven controller-refusal path. The inspector shows unknown
correlation, captured evidence pending projection, or the reconciled receipt
with tenant/actor/resource/assignment/fence/native provenance. Confirmed starts
offer no replay. Stored positive evidence cannot initiate unknown scheduling
retirement or be converted to an unstarted refusal.

Correlation alone does not block existing terminal-task scheduling retirement.
The retirement-preparation CAS and positive-evidence CAS order the two facts in
the same preflight record. If positive evidence commits first, initial
eligibility, the post-provider-await check and atomic preparation all refuse
unknown retirement, preserving the queue. If retirement prepares first, its
immutable `native_outcome=unknown` records knowledge at that preparation time;
a later exact positive is retained and may project the actual native receipt
even after scheduling removal. The inspector shows both historical retirement
provenance and the current positive native outcome. Capture and receipt
reconciliation never reinsert/remove queue entries, resume/cancel execution or
perform a provider action. An already prepared retirement may finish its exact
queue removal under the existing authority/source/scope checks even when a late
positive has arrived; it does not assert current nonstart. The original remains
durable throughout. A retirement prepared before native reservation still blocks
that reservation and the final send guard, so it cannot admit a new native start.

Historical attribution uses the fence and authority observed at send. It does
not require the old lease to remain live, renew a lease, resume a worker or grant
current execution authority. Historical originals lacking exact correlation,
foreign/stale/ambiguous observations and changed snapshots remain unknown. The
existing notification-batch receipt path is separate; this individual-original
capture path does not infer batch outcomes or establish parent #1314 acceptance.

#### Capture window and compatibility

The runtime retains at most 128 response observation slots, each for 120 seconds
from preparation. Slots use opaque unique RPC IDs and a process generation;
retirement clears them, expiration removes them on the next request/response,
and capacity exhaustion prevents another captured send. These are code-owned
protocol safety bounds, not mutable operational definitions. They never extend
a process lifetime. Global native timeout restart and assignment-session timeout
retirement remain unchanged: assignment Codex runtimes disable self-restart and
their wrapper retires timed-out start RPCs. A response can be captured only while
its generation is still being read, or while persistence of an already-read
exact response is in progress. Retired-generation responses cannot resolve later
waiters even if an RPC ID is reused. There is no post-retirement polling or
additional native start.

A crash before the positive-evidence commit may lose a response even if the
provider started. Correlation alone cannot resolve that outcome; the original
remains unknown. After that commit, an independent process can finish receipt
projection from reopened canonical storage without a live transport. This is a
bounded recovery guarantee, not universal positive recovery or negative-start
proof. Storage failure never authorizes replay. Existing evidence
retention/encryption/backup governance applies in the preflight namespace.

Absent fields serialize away, preserving legacy record/API shapes. New readers
accept legacy records; unknown nested schema versions fail visibly. The first
nonempty correlation write is the state-changing compatibility boundary: older
strict preflight readers reject the record. Deployments must declare that reader
restriction through the existing Release/Upgrade compatibility contract, drain
admission and preserve a qualified pre-transition recovery artifact. Source-only
rollback after that boundary is unsupported. Removing correlation/evidence or
restoring an older snapshot must never discard later requests/outcomes. Exact
candidate/main CI, security and recovery-artifact qualification are delivery
evidence; controlled regressions do not establish live-original or deployment
acceptance for #1314.

### Interrupted queue recovery

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
Queued snapshot identity has bounded additive compatibility for
`canonical_ingress_id` and `ingress_execution_profile_definition`: absence and
explicit JSON null are equivalent only for these two optional fields. Non-null
values, unknown fields and all other request/authority inputs remain exact.
Retention and verified replacement-route restoration compare this lossless
projection without parsing away unknown identity data; retained snapshots are
never rewritten. New identity keys fill only these two absent fields with
explicit null, matching the current released (e025/514) serializer/raw-hash reader.
A fresh new-writer hold therefore remains discoverable after rollback, including
both the original key and the exact verified replacement key; no prior-writer
aliases are required. Lookup probes at most four
exact historical hash prefixes (absent/either-null/both-null) in the same existing
index, then checks both the immutable original identity and its verified
replacement projection. The original route remains a retained hold alias after
replacement: enqueueing the same uncertain original through either route must
return its existing preflight refusal, never create another original. Only the
stored replacement Thread and its original-linked reply Thread may be projected;
unrelated route aliases and changed scope, ingress or unknown identity data do
not match, even if an index row points at that retained record. These aliases
suppress duplicates; neither authorizes native admission or resolves an unknown
outcome.
It does not scan the collection, create another index authority or migrate live
records. Historical lookup variants remove only these nullable fields from the
full-null base, preserving every non-null value and unknown key. Old absent,
either-null and explicit-null indexes remain discoverable in either query shape.
Existing pagination within each exact prefix remains unchanged. This bounded
mixed-writer/read and rollback compatibility applies to queue hold discovery;
it does not relax the separate native-correlation or upgrade/drain restrictions
below, authorize old readers to admit unknown originals, or prove runtime recovery.
Fresh-row regressions independently reproduce the released raw-hash lookup and
exercise actual enqueue refusal through both readers/routes without a second
native start or retained-snapshot rewrite.

This compatibility does not alter native admission, retry, reconciliation,
started/unknown holds, receipt generation or public typed state. Snapshot hashes
and full-original comparisons for native correlation, notification membership,
scheduling retirement and concurrent operator/bootstrap checks remain exact:
those bind immutable stored evidence rather than a newly serialized queue model.
The existing queue/preflight inspectors continue to expose canonical state and
recovery actions; no UI adaptation, new definition or model call is required.
This source compatibility alone proves no live-original recovery or installed
release acceptance for #1314.

A native start response without an actual turn ID produces an unknown-outcome
event and retained reconciliation state, never a successful-start receipt.

Terminal WorkItem scheduling retirement is a separate administrative lifecycle
from native-start reconciliation. The administrator/MFA action derives its
target solely from the immutable retained queue snapshot and accepts no caller-
selected WorkItem, repository, tenant, Project or profile scope. It requires the
same-tenant canonical WorkItem to be closed, all retained writable repositories
to remain within that WorkItem's resource scope, the original and replacement
Threads to be idle, and a fresh read of the same authoritative provider object
to report `closed` or `merged`. An open, missing, malformed, cross-scope or
changed object fails closed.

When recovery has moved scheduling to a replacement Thread, retirement resolves
the original Thread through the canonical durable replacement graph under the
retained organization, workspace and Project scope. It projects only the queue
Thread and a reply Thread that originally pointed to that same Thread. Message,
reply provider/conversation/message destination, execution, actor/profile and
repository scope stay byte-equivalent to the immutable snapshot. The original
snapshot and its digest are never rewritten. Resolution is repeated after the
provider read and immediately before removal, so an unverified, conflicting or
concurrently extended route fails closed instead of retiring another entry.

Retirement first appends a `prepared` disposition to the retained preflight.
That disposition fences queue admission and explicit retry while leaving
`reconciliation_required`, the original snapshot, blockers, claim and any
future genuine native receipt intact. A keyed compare-and-remove then removes
only the byte-equivalent scheduling projection of that original entry and
preserves every other entry and its order. Completion records queue-removal
evidence and appends a WorkItem audit event. A crash or conflict after
preparation remains visibly prepared and
non-retryable; queue absence alone cannot promote it to completed retirement.
The inspector therefore distinguishes retired scheduling, incomplete retirement
and the still-unknown native outcome. Scheduling retirement never asserts that
the old native request did not start, supplies an ACK, or replays it.

This extends the application-level persisted preflight payload with an optional
queued snapshot, receipt and scheduling-retirement evidence. Existing records
remain readable by the new application; older strict preflight readers cannot
read new payloads. There is no SQL DDL or retrospective start/ACK backfill.
Rollout requires drained admissions and a declared persisted-payload
compatibility change. Mixed-version
writers are unsupported, and rollback requires the canonical pre-transition
backup of preflights, queues, WorkItems, audit and replacement routing under
maintenance; source-only rollback cannot safely interpret new
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
canonical thread-index recovery state after target settings are saved and
before bot bindings, queued work or archival are retargeted. A restart can
finish an interrupted transfer using the recorded successor without creating
a second bootstrap. The versioned replacement graph retains each original
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

An expected refusal to restore a retained route leaves that queue copy and its
preflight unchanged. Queue draining considers later independent eligible
requests instead of aborting the whole thread. Refused candidates alone do not
schedule another drain. Each external wake checks current canonical provenance
again; a diagnostic throttle never caches admission decisions. Existing bot
event history and the Queue error display expose a fixed refusal code and count,
with at most ten queue IDs in the event. Emission is limited to once per minute
per thread using a process-local cache of at most 256 threads. These diagnostics
do not assert native non-start, add retry authority, or change GET queue status.
Unexpected storage failures still stop admission rather than being classified
as an expected route refusal.

The additive recovery document is ignored by older readers and does not mutate
existing queue or preflight schemas. Downgrading loses durable routing capability
and cannot be treated as a repair for retained work. Deployment and recovery
continue through the canonical upgrade and backup boundaries.


## Retained maintenance refusal recovery

A retained queued request with a matching structured pre-native maintenance
refusal can be reconciled to blocked through the existing administrator/MFA
preflight action. This creates a working explicit retry path even when its
runtime queue copy is missing. Reconciliation preserves the immutable request
and never starts work or establishes delivery. Admission and reconciliation
share the canonical transactional preflight state: an admitting request cannot
be reconciled, and an actual start receipt remains terminal.

The native-start fence clears previous blockers and error text so a later
uncertain start cannot reuse an earlier refusal as negative evidence. Historical
unknown maintenance outcomes still require separate authoritative evidence;
queue absence and stale refusal text are insufficient. Existing inspector
buttons use the canonical action eligibility. FIFO repair and historical
unknown-outcome reconciliation remain separate acceptance under issue #1321.


## Explicit owner retry delivery

An authorized operator retry is a bounded work request, even when its owner is
busy with another issue or MR. The existing scoped bot dispatcher retains that
request in the existing native queue without interrupting active work. Ordinary
background continuity keeps its idle/empty-queue guards. An explicit retry uses
its actual canonical retry attempt for dispatch idempotency and coalescing;
a superseded pending attempt cannot enqueue after a newer retry. Background
continuity cannot overwrite the explicit retry's pending callback.

The Work Item ref, existing owner/profile resolution and original repository
scope continue through the normal dispatcher and admission gates. Queueing
establishes neither native start nor recipient ACK, review, successful CI or
merge. Existing queue/preflight inspectors remain the canonical evidence for
retained requests and actual starts; retry counters alone prove neither.


## Historical replacement evidence recovery

An authorized administrator retry of a retained blocked/failed request may
recover missing predecessor links from the canonical controller event journal.
The bounded query reads retained active, rotated and archived segments and
refuses incomplete or over-budget scans. Routing requires a single complete
recorded chain, matching canonical Project records for archived predecessors
and successors, and a tenant-scoped bootstrap binding for the current target.
Ambiguity, cycles, missing metadata and another tenant fail before a retry claim
or queue mutation. The verified chain is persisted in one transactional graph
update; existing bootstrap edges and provenance are never overwritten.

Legacy events cannot establish an original bootstrap actor or profile revision.
Recovered provenance records those fields as unknown and retains event hashes,
segments and the actual administrator who restored the route. The retained
request still supplies its immutable original actor, profile revision and exact
repository scope for normal admission. A routing link and a current bootstrap
binding are neither authority nor native-start/recipient delivery evidence.
Existing preflight original/replacement links report the admitted retry target;
historical unknown-start outcomes remain outside this retry path.


Event dispatch also retains a request when the runtime start lock explicitly
refuses it with `thread_turn_already_active` for the same thread, including a
verified stale-thread replacement. This refusal occurs before native admission.
The existing queue preserves the original Work Item, profile and repository
scope; idle-only background dispatch still skips. Other conflicts, another
thread and unknown native-start outcomes do not establish this retry condition.

Resource validation reports disk-lock wait, execution disk scan, read-only workspace
metadata lookup and process-tree RSS durations separately, including failed stages.
Skipped stages reset to zero on each resource validation. Disk enumeration checks
the existing cooperative operation budget within each directory entry and before
returning; cancellation cannot produce a partial-byte success or refresh the disk
cache. These diagnostics do not extend the supervision deadline or establish the
cause of a previously aggregated timeout.

### Pre-turn archived resume evidence

The retained-request preflight domain also admits a distinct version-1
`queued_resume_correlation`/`queued_resume_evidence` pair. Its assignment-bound
transport prepares before `thread/resume`, runs the existing current
worker/identity/lease/fence and admission guards inside the native write lock,
and observes only that process generation's matching response ID. The runtime
version comes from the same process's initialize `userAgent`; only the qualified
`codex_cli_rs/0.160.0` version is accepted. An unknown version refuses captured
resume before send, not permission to guess a newer error contract.

Only the exact qualified `-32600` archived-session response for that native
session and method is negative proof. The same error text from another RPC,
partial/mixed/foreign results, missing responses, transport failure and timeout
remain unknown. Positive resume proves no turn was started; a later captured
`turn/start` requires a matching positive resume observation. No raw response
text is persisted. The original queue snapshot, actor/profile/resource scope,
claim, execution, assignment and fence remain pinned separately from the native
session and transport generation.

The trusted reader stores the observation without automatically reconciling or
retrying. The existing authenticated admin/MFA `reconcile-prestart` inspector
consumes a current exact archived refusal with no positive/start/retirement
conflict, atomically blocking the retained request. It makes no provider call.
An explicit later retry uses the existing verified replacement graph and current
admission, retaining up to eight complete prior resume observations; exhausted
history refuses another send. Legacy uncertain attempts have no new proof and
are never retroactively classified from strings or current archive flags.

Absent resume fields serialize in the old shape. First capture persists the
new typed fields; old strict readers/writers reject those records rather than
silently discarding their safety evidence. A source-only rollback after capture
is not claimed compatible. Observation schema/runtime versions fail visibly.
The existing preflight inspector renders generation/RPC/assignment/fence/session
and snapshot digest, distinguishing archived refusal, successful resume and
unknown outcome. Its buttons consume canonical eligibility; display text grants
no retry or authority. This source slice does not recover historical routing
without original authoritative replacement evidence or prove original delivery.

A successful correlated resume may precede a typed local controller refusal
before input. Only that existing local exception boundary supplies the internal
pre-input witness; provider HTTP detail codes cannot supply it. With the exact
claim/snapshot and complete successful resume observation, and no prepared
turn/start, the disposition transaction moves the resume observation into its
bounded history before preserving the controller's queued/blocked state. It
never discards evidence or infers nonstart from absence alone. Missing, archived,
foreign, incomplete or uncertain resume results, any prepared turn/start, stale
claims and exhausted history retain reconciliation-required custody. Actual
provider failures after send remain unknown even when their text matches a
controller refusal code. No replay or permission is conferred by the fixture or
this retained local-witness history.

When the resume writer's admission guard itself refuses before `stdin.write`, it
commits the exact no-write blocker/correlation before emitting the internal
`NativeStartAdmissionRefused` witness. The earlier resume exception boundary
validates that committed witness atomically against the same original/claim,
SDK method/version/generation/session/fence and no possible user-input proof,
then propagates only the typed local controller cause to the ordinary drain.
It does not manufacture a provider resume result. Failed bookkeeping, missing or
conflicting witness, any possible input reservation or positive receipt remains
owned; no absence-only or provider-text inference releases custody. Queue
restoration uses the committed disposition/claim, so a concurrent positive
start, changed claim or retirement wins over the earlier exception. Original
snapshot, queue position and retry counter remain unchanged on known refusal.

## Settled native writer and process lifecycle

Native JSON-RPC writes retain the existing serial writer lock while one settled
worker operation performs final canonical authority/reservation validation and
writes/flushes the message. The guard is not an asynchronously cached permission:
its current checks and the actual write remain in the same operation. Slow
storage or a full native pipe leaves the event loop available for lease
supervision and reader responses. Repeated caller cancellation joins actual work
before releasing writer ownership; cancellation never proves that input was not
written and never requests a resend.

The writer captures the exact process, stdin and runtime generation. It validates
that capture both before and after the guard, then writes only to the captured
stdin. Retirement during a slow guard prevents sending to the retired process or
borrowing a replacement's pipe. An already active partial write can remain
uncertain after termination; only the existing immutable reader receipt or a
qualified no-write guard witness establishes its stronger outcome.

Stop immediately fences and terminates the captured process before waiting for
lifecycle ownership. This also interrupts initialization without deadlocking its
lifecycle lock. Lifecycle-owned cleanup uses the existing termination/kill
bounds, then joins the settled writer before completing retirement or publishing
a replacement. Kill escalation precedes waiting for writer ownership, because a
full pipe must not prevent termination of an unresponsive native process. Reader
and pipe-executor cleanup retains its original generation's references. Internal
startup and timeout paths reuse the lifecycle-owned stop method instead of
reentering the public lifecycle lock.

The pending-RPC and start/resume capture contracts are unchanged. A real positive
reader receipt remains authoritative if a caller is cancelled during flush.
Partial-write errors retain unknown outcome and exact correlation; neither a
transport exception nor a retired process creates retry authority. UI consumes
the existing runner, queued-start, refusal and reconciliation projections.

## Bounded current-owner continuation after runner loss

An actionable canonical Work Item with a terminal, unleased native assignment
can receive one **new** current-work request through the existing autonomy
controller and retained-turn admission domain. This is distinct from retrying an
old native input or reviving its lease. The request retains the current Goal,
responsible bot binding, exact Agent Profile/actor, execution profile, repository
scope, failed assignment/fence and an affirmative stopped-process/settled-executor
observation. A missing process handle without that earlier observation is a
visible reconciliation blocker, not proof of teardown.

The deterministic request identity binds tenant, logical bot binding, Work Item
and its current business intent. Changed ownership, stage, repository scope,
provider revision, handoff or next action changes that intent;
runtime delivery, usage, workspace and timestamp bookkeeping do not. A committed
preflight reservation survives restart, uncertain native start and repeated
failure observations. There is no cooldown-based replay or automatic reset of
this budget. Reservation or queue-append interruption requires inspection of
that retained original rather than constructing another request.

Fresh queued/in-flight inputs keep ownership of the lane. Recovery appends at
the durable queue tail and uses the existing admission priority policy; it never
steers an active human turn. Historical uncertain inputs for unrelated Work Items
remain byte-for-byte retained. An unresolved native outcome for the **same** Work
Item blocks automatic continuation. A positive START receipt also does not prove
that its later native turn completed. This recovery path has no qualified
generation-bound terminal witness for such prior same-item turns, so it exposes
`owner_recovery_prior_turn_unresolved` for explicit reconciliation and can select
another independent owned item. It never derives completion from usage, model
text, stopped custody or synthetic transcript recovery. Existing preflight and queue surfaces expose
the recovery provenance, exact blocker and recovery action owner.

Worker admission, native history resume and actual input still cross canonical
current authority, Profile, resource, maintenance, fenced lease and guarded
start/resume capture checks. Current Work Item intent, binding, Goal and scoped
autonomy controls are revalidated before native input. A stopped historical turn
or a transient `reconciliation_required` admission is never classified as a
successful or failed business execution from display state alone.

Compatible native custody can acquire a fresh worker through ordinary admission.
An initial-process, incompatible-scope or missing-prior-custody refusal remains
in the existing preflight domain with its qualified fresh-bootstrap operator
action; this continuation path creates no replacement threads and transfers no
old queue. No new model-visible administrative recovery capability is granted.
The deterministic watchdog has zero model calls for duplicate observations,
blocked recovery or genuinely idle lanes. A useful continuation still uses the
existing Goal/Work Item model accounting and execution budgets.


The current thread is resolved through the complete tenant-scoped durable
replacement graph, without retargeting bindings or moving predecessor queues.
A subsequent graph change revokes the retained request at guarded admission.
Explicitly cancelled custody produces one retained operator-owned blocker;
automatic continuation cannot undo an intentional resource release.

Native project-delegation Goals without a qualified independent current Work
Item receive a deduplicated exact-snapshot blocker and canonical event on the
existing Goal execution projection. This observer never invokes application-mode
Goal continuation or a model. It preserves Goal status, delegation authorization,
leases, recovery budgets and all provider outcome fields, and names the existing
execution owner responsible for reconciliation. A stale observation cannot
replace a newer binding or an existing unrelated stop reason.


A recovery admission may roll the thread onto a fresh canonical bootstrap. Its
already terminal, unleased, positively stopped source assignment remains
unchanged: only its local process handle is settled and unregistered. Ordinary
runtime switching retains its existing retirement behavior. The recovery's
source assignment and fence participate in the atomic bootstrap rebind compare
and swap. An admission-local witness links only that invocation's retained
claim, unchanged failed source and newly created successor. It rechecks the
current bootstrap, successor identity, Profile, actor, resources, worker, fresh
lease and fence at the native resume/input boundary. Current Work Item, Goal,
authority and pauses are still read on every validation; a new pending human
input takes precedence. The witness is never restart authority: a later drain
cannot recreate it to accept a cancelled or unrelated successor. External
source cancellation, changed custody, revoked claims or scope leave the exact
retained original blocked on the existing operator surfaces, without replay.
