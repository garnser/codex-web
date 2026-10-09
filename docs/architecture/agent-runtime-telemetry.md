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
