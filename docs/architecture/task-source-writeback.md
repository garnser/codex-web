# TaskSource writeback coordination

Canonical Work Item state remains authoritative. External task systems receive a
projection of selected canonical fields through one keyed writeback coordinator
per authoritative source identity.

## Coalescing

Asynchronous writeback requests are keyed by tenant/workspace plus
TaskSource type, source instance, and external item ID. Repeated updates replace
the pending desired snapshot instead of creating another provider task.

One event-loop turn is intentionally yielded before the first provider read so
synchronous bursts collapse to the latest desired owner/stage. A generation
counter is checked after the provider read; a superseded generation is not
mutated externally and the coordinator immediately processes the newer desired
state.

## Provider-efficient projection

Adapters may implement the optional combined-write contract. The coordinator
then performs:

1. one provider read
2. a generation check
3. one combined projection of owner + canonical stage
4. zero provider writes when the provider already matches
5. at most one provider mutation when either/both fields differ

The GitLab adapter implements this contract by editing owner/status labels and
open/closed state in one issue update.

Adapters without the combined capability continue through the provider-neutral
owner/state write contracts while still benefiting from keyed task coalescing.

## Canonical-state safety

Provider response metadata is merged into the latest canonical Work Item record.
An older scheduled copy is never saved wholesale over newer canonical
owner/stage state.

Provider revision participates in the applied fingerprint. This prevents
feedback loops for unchanged provider revisions without suppressing
reconciliation after an external provider change.

## Failure and retry

Each keyed coordinator task owns bounded exponential retry. HTTP 429
`Retry-After` is honored up to the configured safety cap. Provider failure does
not roll back canonical Work Item progress. The latest desired canonical state
remains queued until success or the bounded retry budget is exhausted; a later
canonical update creates a fresh reconciliation opportunity.

## Observability

`GET /api/operations` exposes TaskSource writeback state including:

- scheduled/coalesced/skipped/applied/retried/failed counts
- provider read/write counts
- pending queue depth and oldest pending age
- active coordinator task count
- current backoff count
- per-item desired/applied owner, stage, and provider revision
- bounded last-error summaries

The coordinator is lifecycle-owned and cancelled during controlled application
shutdown.
