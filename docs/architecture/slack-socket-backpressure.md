# Slack Socket Mode backpressure

Slack Socket Mode ingress uses bounded, connection-local worker shards rather
than creating one asyncio task for every received envelope.

## Admission and acknowledgement

For each Slack connection:

1. parse and normalize the Socket Mode envelope using the registered provider
2. commit the canonical conversation event and a PENDING projection receipt
   containing the normalized input, original ingest identity, tenant, connection,
   project, and transaction-assigned retention sequence
3. schedule pending receipts in that sequence through the bounded worker shards
4. acknowledge the envelope after durable retention succeeds

Storage failure is not acknowledged, even when an in-memory deduplication key
exists. A full memory shard leaves the receipt PENDING and is safe to acknowledge:
the existing runtime synchronization recovers it after capacity returns or after
restart. Live arrivals use the same recovery path, so they cannot bypass older
retained inputs. Approval interactions run the existing canonical resolution
handler before ACK; credential-bearing interaction response URLs are not retained.

## Worker and fairness model

Each connection owns a fixed number of workers. Every worker owns one bounded
queue. Channel ordering keys are assigned to workers with a stable SHA-256
hash.

This gives the runtime the following properties:

- the number of asyncio worker tasks is independent of burst size
- payload memory is bounded by worker count × per-worker queue capacity
- all payloads for one channel remain ordered on one shard
- one hot channel can occupy only its assigned worker/shard
- unrelated channels mapped to other shards continue to make progress
- unrelated channels that hash to the same shard share that shard fairly in
  FIFO arrival order; the design does not promise one worker per channel

The runtime does not create waiting tasks for queued payloads.

## Deduplication

Accepted event identities are tracked in bounded in-memory history and a separate
in-flight set. History alone cannot suppress a still-PENDING durable receipt after
a pre-claim failure. Canonical event idempotency and atomic PENDING-to-ROUTING
claims prevent duplicate routing. Recovery samples up to 100 pending inputs per
configured shard (at most 32 shards), in retained arrival order; a hot shard does
not exclude the sample for another shard.

Recovery pins the original ingest identity, tenant/workspace, connection and
project. Current provider lifecycle and routing authority are checked again.
ROUTING and uncertain receipts are never blindly replayed; they require
reconciliation. Pending and uncertain retained inputs are exempt from completed
receipt history pruning. This retains input, but does not assert successful
execution or exactly-once downstream effects.

## Overload behavior

When one shard reaches capacity:

- the payload is rejected from local admission
- the Slack envelope is acknowledged only after canonical retention succeeds
- pending routing remains recoverable in canonical conversation state
- the runtime emits an overload telemetry event
- bot/runtime status exposes the overloaded state and queue metrics

A retention failure emits a separate diagnostic and leaves the envelope unacknowledged.

## Shutdown

Workers and queues are tracked explicitly. On controlled shutdown the runtime
first allows queued work to drain for a bounded interval. Remaining queued and
active work is cancelled explicitly, counted, and reported through telemetry.
No orphan payload tasks are left outside the managed worker set. Never-started
PENDING inputs survive cancellation; claimed inputs remain visible for
reconciliation if the downstream outcome is unknown.

## Observability

Bot status exposes per Slack connection:

- queue depth and configured capacity
- oldest queued age
- active worker count and worker limit
- bounded per-channel backlog summary
- accepted, deduplicated, rejected, processed, failed, and cancelled counts
- latest processing latency
- current overload state and last overload time
- current deduplication-retention size
- durably retained envelope count and bounded canonical pending sample

Conversation receipt APIs/UI expose outcome, original ingest identity, target
project and whether input remains retained, without exposing retained message
bodies. Recovery errors expose an exception class rather than raw provider input.

These fields describe transport/runtime pressure only. They do not establish
canonical message processing success.

## Configuration

The worker model is controlled through bounded environment settings:

- `CODEX_WEB_SLACK_PAYLOAD_WORKERS` (default 4, range 1–32)
- `CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER` (default 256, range 1–10000)
- `CODEX_WEB_SLACK_PAYLOAD_DEDUPE_LIMIT` (default 50000, range 1000–200000)
- `CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS` (default 5, range 0–60)

Increasing queue capacity raises worst-case retained payload memory. Increasing
worker count raises downstream concurrency. Deployment tuning must keep both
within the provider/runtime capacity budget.
