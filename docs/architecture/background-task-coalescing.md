# Coalesced in-process background work

codex-web has two different scheduling concerns:

- **Durable SchedulerService** owns persisted future/recurring timers and emits
  canonical events with leases, misfire handling and restart recovery.
- **KeyedTaskCoordinator** owns already-triggered in-process coroutine work that
  must be bounded and coalesced while the process is alive.

The second primitive must not become another source of durable scheduling truth.

## Keyed coordinator

A coordinator accepts a semantic key, coroutine factory, optional semantic
revision, scope and timeout. It guarantees:

- at most one runner task per semantic key
- while a key is running, repeated triggers retain only the latest pending work
- global concurrency is bounded
- per-scope concurrency is bounded
- each run may have an explicit timeout
- cancellation, timeout, failures and coalescing are observable
- controlled shutdown owns and cancels every tracked task

A key is an execution-coordination identity, not canonical domain state.
Callers must re-read canonical state before external side effects when the work
can become stale.

## Native recovery

Native recovery assigns one key to each recovery cycle:

```text
native-recovery:<cycle-index>
```

Independent cycles may run concurrently, but the same cycle cannot overlap
itself. If a slow cycle is still running when a later recovery trigger arrives,
the later trigger becomes one coalesced rerun. Additional triggers replace that
pending rerun rather than creating more tasks.

Each recovery run has a bounded timeout. Runtime operations expose the last
trigger/reason, running/pending state, coalesced count, timeouts, failures and
run durations.

The existing recovery cooldown remains an admission policy for trigger
frequency. It is no longer relied upon as an overlap-prevention mechanism.

## Work Item continuity dispatch

Immediate structured-handoff and actionable-owner dispatch use keys scoped to
the Work Item operation:

```text
handoff-dispatch:<work-item-ref>
owner-dispatch:<work-item-ref>
```

Rapid state transitions therefore retain only the newest pending dispatch for
each operation. Global and per-Project concurrency limits prevent many
independent Work Items or Projects from creating an unbounded task set.

The scheduled semantic revision contains the owner/stage or handoff recipient
and request revision. The task re-reads canonical Work Item state before doing
work, and the dispatch method re-reads it again after asynchronous thread
replacement immediately before the external dispatch boundary.

If owner, stage, closure state, handoff status, recipient or requested-at
revision no longer matches, the stale dispatch is discarded.

Existing watchdog dispatch keys remain the durable/idempotent side-effect guard.
The in-process coordinator does not replace those keys.

## Delayed continuity checks

The existing delayed handoff/actionable-owner continuity checks remain
cancel-and-replace tasks keyed by Work Item reference. They already have a
single tracked task per Work Item and are included in continuity status and
controlled shutdown.

## Configuration

Runtime policy exposes bounded controls:

- `CODEX_WEB_NATIVE_RECOVERY_CYCLE_TIMEOUT_SECONDS`
- `CODEX_WEB_CONTINUITY_DISPATCH_TIMEOUT_SECONDS`
- `CODEX_WEB_BACKGROUND_TASK_MAX_CONCURRENCY`
- `CODEX_WEB_CONTINUITY_PER_PROJECT_CONCURRENCY`

The defaults preserve local-first operation while preventing unbounded
concurrency.

## Observability

`GET /api/operations` reports:

- `runtime.nativeRecovery`
- `runtime.continuityBackground`

Coordinator telemetry includes queue depth, active/running work, max observed
concurrency, scheduled/coalesced/completed/failed/timed-out/cancelled counts,
oldest pending age, desired/completed revisions and per-key duration/error
metadata.

## UI impact

This changes runtime execution pressure rather than introducing a user-managed
domain object. Existing Work Item behavior remains unchanged. The privileged
operations surface is the operator inspection path; no separate configuration
UI is required.
