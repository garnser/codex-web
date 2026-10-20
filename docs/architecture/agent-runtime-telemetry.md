# Agent runtime telemetry and conformance

Issue: #353.

Agent execution usage is a canonical runtime concern, not a single ModelGateway invocation. A provider-backed agent session can make multiple model calls, invoke tools, edit files, compact context, spawn subagents and survive provider-native session identifier changes. codex-web therefore stores a compact runtime-usage projection keyed to canonical AgentSession/execution identity.

## Canonical usage record

AgentRuntimeUsage records:

- tenant/workspace/project attribution plus optional Goal, Work and Decision identifiers;
- canonical AgentSession, execution, assignment and execution-workspace identifiers;
- provider/runtime/type and capability revision;
- provider-native session, turn and request identifiers as compatibility metadata only;
- observed model IDs and provider runtime version where exposed;
- input/output/cache/reasoning token measurements, cost and duration only when the runtime exposes them;
- bounded tool, shell, file-edit, Git and compaction counts;
- subagent/model-call counts only when observable;
- terminal outcome and telemetry completeness: exact, partial or unavailable.

Unknown values remain null. codex-web never derives fake cost or token precision from unrelated counters.

## Provider normalization

The telemetry service subscribes to AgentRuntimeEvent from each registered adapter. Adapter-native payloads are interpreted only long enough to extract bounded canonical measurements; the provider transcript is not persisted.

Current normalization includes:

- Codex thread token-usage notifications, using the turn-local last breakdown rather than cumulative session totals;
- Claude result usage, cache token fields, total cost, duration and observed model/runtime version;
- canonical tool/shell/file/Git counters from normalized tool activity;
- terminal outcomes from successful, failed and interrupted runtime events.

Provider events are fingerprinted before aggregation so duplicate/replayed events do not double-count usage or evidence.

Canonical sessions are stored as keyed records with a transactional secondary
index over provider, runtime, and provider-native session identity. Telemetry
resolves only the matching index entries and canonical records; its cost does
not grow with unrelated session history. The resolver fails closed when a
native identity is ambiguous across tenant scopes or when an index entry is
invalid. The former aggregate document remains a rollback checkpoint and is
refreshed by the normal compatibility-state flush; it is not a second active
authority.

Tenant-scoped native lookups support independent provider and runtime filters.
Each index alias must match its canonical record's complete encoded identity;
valid aliases outside a partial filter are skipped, while corrupt aliases fail
closed even when the filter would otherwise exclude their target.

## Evidence

### Current custody after admitted same-thread resume

An assignment-bound native resume captures its existing canonical AgentSession
before the provider await. Capture validates the same assignment, worker,
identity, exact profile, binding, workspace and lease invariants under canonical
transaction locks without changing custody or emitting a reassociation event.
Known incompatible prior owner, tenant or repository scope refuses before any
provider resume or Turn input. This validation uses the owned worker executor;
the native writer retains its existing live assignment and admission guards,
without another catalog transaction on the native event loop. Only successful
resume may invoke the internal custody projection hook. The hook revalidates the trusted worker session and
requires exact tenant/Project/provider/runtime/native-thread identity, current
bootstrap binding, assignment/worker/fence/deadline, workspace and lease, and
published execution/Profile Definition pins. Compare-and-swap refuses a
concurrently modified session. Legacy adoption and public session-resume APIs
do not gain permission to rebind from caller-supplied assignment identifiers.

A missing prior assignment also refuses before provider resume. The controller
retains a distinct `native_session_prior_assignment_missing` proof for the new
request, including its exact admission claim, immutable input digest, session,
source bootstrap and admitted assignment/fence. This does not establish the
prior execution's outcome or authorize same-session resume. After the refused
new assignment is terminal and fenced, existing administrator bootstrap recovery
may create one fresh session at that request's exact scope. Recovery checks that
the prior assignment remains absent before creation and again under the source
assignment locks at final route publication. A reappearing prior record, changed
source custody, unknown/resumed original or stale input digest refuses recovery.
Existing uncertain inputs and historical session/usage records remain unchanged;
the existing preflight blocker and recovery inspector show the reason and action.

A pending initial native session that belongs to a previous process refuses
before resume or Turn input. The controller emits its exact
`initial_bootstrap_process_lost` claim/snapshot proof only after awaiting canonical
bootstrap cancellation and process cleanup. Explicit administrator bootstrap
recovery can use that proof even when the source remains an unarchived current
bot binding. It verifies the exact pending bootstrap, cancelled/fenced assignment
without a lease, original actor/Profile and repository/execution scope. The
operator and unchanged source assignment are checked again under the existing
atomic source-binding, assignment and replacement-graph publication locks.
Native input is still a separate guarded retry of that same retained original;
creating custody is not delivery. No archive, binding retarget, whole-queue move,
historical-unknown replay or automatic revival of cancelled work is implied.
The existing preflight inspector and recovery action expose this operation.

The current session row and a metadata-only
`agent_session_execution_reassociated` event in the existing worker audit
collection commit together through canonical StateStore cross-domain mutation.
The event identifies previous custody and the admitted fence/Definition. Old
assignments, workspace custody, RuntimeUsage and prior receipts remain unchanged.
The project-coordination delegation guard continues requiring exact current
session/assignment joins; neither global pause nor an operator Goal binding
relaxes a stale join. No continuation engine is added.

This uses existing session and WorkerEvent schemas and existing record storage;
no schema migration or catalog Definition change is introduced. Older binaries
can read the same records but do not perform reassociation; a downgrade therefore
does not promise continued rollover functionality. Resume failure/unknown outcome
does not advance custody. Successful same-custody resume also recovers failed or
closed current status to ready and clears current failure fields, without
rewriting the earlier reassociation event. Repeated ready-state recovery is a
no-op. Cancelling the owning turn, including all-task shutdown or repeated
cancellation, drains the actual concurrent executor Future before returning,
not merely a cancelled asynchronous wrapper. Production work uses the existing
worker-session executor and pending-work custody set; standalone lifecycle
callers own and join a bounded one-worker executor. Existing session/runner/worker/Goal UI
must distinguish current custody from immutable prior execution evidence and
expose stale-join denials. That UI and installed end-to-end qualification remain
part of the issue's acceptance; source tests alone cannot prove them.

The read-only Session trace projects each RuntimeUsage record's own assignment,
execution and execution-workspace identifiers, including null attribution. The
existing trace UI compares the complete recorded tuple with current session
custody and displays each event separately. Missing identifiers remain unknown;
they are never inferred from current custody. This additive API projection does
not change persisted records, receipts or storage compatibility.

Custody fault coverage uses the same service contract on SQLite and the isolated
PostgreSQL CI backend. Independent connections compete from the same captured
Session to prove a single CAS winner; a database-triggered event-write rejection
checks rollback of the preceding Session delta and visibility from another
connection. Legacy adoption remains an idempotent lookup, followed by a separate
trusted resume hook before native input. Tests without the isolated PostgreSQL
test DSN are skipped and provide no PostgreSQL qualification; the required
distributed-backends job explicitly runs the custody integration module.

Terminal runtime outcomes emit compact EvidenceType.RUNTIME_RESULT records linked to project/work/execution scope. Evidence contains identifiers, result, telemetry quality and bounded counters; it does not contain prompts, provider transcripts, shell output or raw model responses.

Runtime usage telemetry is operational/evidentiary data only. It cannot grant authority, approve tools, change an ExecutionAssignment, or mark an external side effect successful.

## Conformance

The shared AgentRuntime conformance suite is run against both Codex and Claude adapters. It validates the common contract for capabilities each adapter claims:

- capability declaration;
- create/resume/execute/interrupt/close lifecycle;
- terminal event projection;
- canonical approval-response transport;
- recovery/shutdown;
- fail-closed unsupported native capabilities.

Assignment-bound process tests separately exercise the shared worker boundary for lease/fence changes, runtime revision mismatch, credential rotation/expiry, sandboxed process execution and termination. Telemetry tests cover replay deduplication, attribution, quality markers, redaction and provider-specific usage normalization.

Provider-specific fixtures remain at the adapter edge. Assertions over canonical usage, identity and lifecycle remain provider-neutral.

### Usage inspection scheduling

The agent-session list projects all authorized sessions from one fresh,
tenant-filtered telemetry read per request. That awaited worker-pool operation
selects each visible session's latest record using the existing descending
observation-time and record-ID order. Runtime health is read once for each
provider/runtime pair within the response, including unavailable fallbacks.
Neither snapshot survives the request; subsequent requests see fresh usage and
health. Empty session lists skip telemetry reads. The single-session endpoint
retains its existing scoped read. Response schemas, accounting, session
authorization and canonical telemetry validation remain unchanged.

The runtime usage API resolves its authenticated request actor before scheduling
one awaited worker-pool read. That operation retains the existing validated catalog
lookup, tenant and request filters, resource aggregation, output order and JSON
projection together, with the request ContextVars. Invalid catalog data and read
failures still fail visibly; an unauthenticated request never starts the read.
Cancellation stops the response task; an already executing synchronous read may
finish without producing a response. This scheduling boundary prevents synchronous
usage inspection from blocking ASGI and native RPC scheduling. It does not reduce
the catalog's total validation, CPU or database work, and introduces no cache or
storage migration.
