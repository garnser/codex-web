# Task-source writeback coordination

Canonical Work Item state remains authoritative inside codex-web. Projection
back to an external TaskSource is asynchronous and best-effort; provider
metadata returned by a write is merged back without replacing newer canonical
owner or lifecycle state.

## Scheduling

Writeback is keyed by authoritative external task identity:

```text
(source type, source instance, external id)
```

Only one writeback task runs for a key at a time. Repeated Work Item updates
replace the pending desired snapshot with the newest canonical state rather
than spawning one asyncio task per update. A burst therefore has bounded
concurrency and naturally coalesces superseded revisions.

The coordinator records:

- scheduled and coalesced requests
- applied and skipped projections
- retry and failure counts
- provider read/write counts
- active and pending work
- oldest pending age
- last desired/applied revision per external task
- current bounded retry/backoff state

Admin operators can inspect this through
`GET /api/work-items/task-source-writeback`.

## Delta-driven projection

Adapters may implement the additive
`TaskSourceProjectionWriteCapable.write_projection()` contract when their
provider can shape owner and lifecycle changes together.

The GitLab adapter uses this contract to:

1. read the issue once
2. derive owner and status-label deltas from that one snapshot
3. skip the mutation completely when labels/state are already synchronized
4. combine owner and lifecycle label changes into one issue update
5. include close/reopen state transitions in that same update

Thus a normal owner+stage change requires one GET and at most one PUT rather
than independent GET/PUT pairs for owner and state.

Adapters that do not implement combined projection retain the existing
capability-gated owner/state methods. The coordinator still performs an initial
read and skips operations whose canonical projection is already equal.

## Concurrency and stale completion

A provider request can complete after a newer Work Item update. Provider
completion must never replace the scheduled Work Item snapshot wholesale.

Returned provider identity/revision and labels are therefore merged into the
**latest canonical Work Item record**. Owner, stage, handoff, execution and
other canonical fields remain from that latest record. If a newer desired
revision arrived while the provider request was in flight, it remains pending
and is projected in the next coordinator pass.

This prevents stale provider completion from reverting newer canonical work.

## Retry behavior

Transient write failures use a bounded retry loop with exponential backoff.
A newer pending canonical revision supersedes retrying an older revision.
After the retry budget is exhausted, the failure is surfaced through writeback
metrics and the existing event sink. Future canonical changes or normal
TaskSource reconciliation can schedule a fresh projection.

Provider writes must remain idempotent with respect to the desired projection:
re-reading an already synchronized provider state produces no mutation.

## Feedback-loop rule

Saving returned provider metadata is a repository-level metadata merge, not a
new canonical progress transition. It does not call the Work Item progress
scheduler. Provider webhook reconciliation may update provider metadata, but
an unchanged owner/stage projection is skipped, so provider responses cannot
create an owner/state writeback loop.

## UI impact

This is an operator/runtime performance change and does not introduce a new
user-managed domain object. Existing Work Item UI behavior is unchanged.
Operational visibility is provided through the admin writeback-status endpoint;
no separate configuration UI is required.
