# Durable scheduler, timers, and deterministic time

## Status

**Milestone 7 scheduler contract (#158).** Time-triggered work is represented
as durable canonical schedule state. The scheduler is a deterministic engine:
it never invokes an LLM, ActionProvider, or privileged execution path directly.

## Flow

```text
Create one-shot / recurring schedule
        ↓
Persist canonical schedule + next_run_at
        ↓
Claim due schedule with fenced lease
        ↓
Apply deterministic misfire/catch-up policy
        ↓
Emit idempotent canonical schedule.due event
        ↓
Advance durable next-run state / terminal state
        ↓
Normal canonical event filtering and reasoning gate
```

Scheduled work therefore enters the same orchestration boundary as external and
user-triggered events. A timer firing can cause reasoning only if downstream
deterministic filtering and autonomy policy explicitly allow it.

## Durable state and ownership

Schedules are stored in the shared SQLite state store under the `scheduler`
namespace. Each record carries a stable schedule ID, tenant/workspace scope,
trigger type, payload, status, next/last timing information, revision, and
lease metadata.

A scheduler worker claims due records transactionally. The claim increments
the record revision and installs a bounded lease. Completion requires the same
lease owner and revision, which fences stale workers. An expired lease can be
claimed by another scheduler worker. This is the single-instance implementation
of the ownership contract that future replicated coordination in #142 must
preserve.

Pause, resume, and cancel operate on the same durable record. Cancelled and
completed schedules have no future firing.

## Crash recovery and duplicate suppression

The important crash window is:

1. a worker claims a due schedule;
2. it publishes the canonical event;
3. the process dies before advancing `next_run_at`.

After lease expiry another worker reclaims the same due occurrence and publishes
the same source/idempotency pair. The canonical event store returns the original
event and performs no second subscriber dispatch. Only then is the schedule
advanced. This gives restart recovery without a second side-effect path or an
in-memory exactly-once claim.

## Recurrence and wall-clock time

Two recurring modes are code-owned engine semantics:

- `interval` — fixed elapsed seconds between occurrences;
- `daily` — an IANA timezone plus local wall-clock time.

Daily recurrence is calculated in the named timezone for each date instead of
adding 24 elapsed hours. A 09:00 Europe/Stockholm schedule therefore remains at
09:00 across DST transitions even when the UTC interval is 23 or 25 hours.
Ambiguous fall-back wall times use fold 0 deterministically. Non-existent
spring-forward wall times follow the standard-library timezone normalization
for that local timestamp.

The initial `due_at` is authoritative for the first occurrence. Subsequent
daily occurrences follow the configured local wall-clock definition.

## Misfire and catch-up policy

Downtime, clock jumps, or delayed processing can make one or more occurrences
overdue. Every schedule declares one policy:

- `skip` — emit no overdue event and advance past the backlog;
- `fire_once` — emit only the latest overdue occurrence;
- `bounded_catch_up` — emit only the most recent occurrences up to the
  schedule's catch-up limit.

A single occurrence that is within the configured misfire grace period fires
normally. Per-tick claim and event caps provide an additional backpressure
boundary so restart or clock-jump recovery cannot create an unbounded event
storm.

## Runtime behavior

The central runtime supervisor owns one scheduler task. Durable timing truth is
always read from the state store; the in-process wake event/timeout only avoids
busy polling. Startup immediately evaluates due state, so correctness does not
depend on a process-local sleep surviving restart.

Idle or future-only schedules invoke no models and emit no events. Scheduler
tick failures are logged and retried from durable state.

## API and UI impact

The canonical API exposes list/create/get/pause/resume/cancel under
`/api/schedules`. Reads require scheduler read/admin authority; mutations
require scheduler admin authority and MFA for human administrators.

The backend intentionally does not create schedule-specific shadow UI state.
#112/#125/#127 own the operator projection for schedule owner/scope, timezone,
recurrence, next/last firing, misfire policy, generated canonical event,
pause/resume/cancel controls, and schedule-to-result provenance.
