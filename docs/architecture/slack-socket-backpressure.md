# Slack Socket Mode backpressure

Slack Socket Mode ingress uses bounded, connection-local worker shards rather
than creating one asyncio task for every received envelope.

## Admission and acknowledgement

For each Slack connection:

1. parse the Socket Mode envelope
2. derive a bounded deduplication key from the canonical Slack event identity,
   falling back to the envelope identity when needed
3. derive an ordering key from the Slack channel/conversation
4. map the ordering key to a stable worker shard
5. admit the payload with `put_nowait()`
6. acknowledge the Slack envelope only after admission succeeds, or when the
   envelope/event is already safely deduplicated

A queue-full admission is **not acknowledged**. Slack may therefore retry the
envelope instead of codex-web claiming success and silently discarding a user
message.

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

Accepted event/envelope identities are retained in a bounded in-memory set plus
FIFO expiry order. Reconnect/replay of an already admitted event is acknowledged
without creating duplicate downstream work.

The retention limit is configuration-bounded. It is transport deduplication,
not canonical business-state authority; downstream canonical ingestion remains
responsible for its own durable idempotency.

## Overload behavior

When one shard reaches capacity:

- the payload is rejected from local admission
- the Slack envelope remains unacknowledged
- the runtime emits an overload telemetry event
- bot/runtime status exposes the overloaded state and queue metrics

There is no silent drop-after-ACK path.

## Shutdown

Workers and queues are tracked explicitly. On controlled shutdown the runtime
first allows queued work to drain for a bounded interval. Remaining queued and
active work is cancelled explicitly, counted, and reported through telemetry.
No orphan payload tasks are left outside the managed worker set.

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
